from collections import defaultdict
import datetime
import dateutil
from dateutil import relativedelta
import json
import logging
import os

import requests
import pymongo

from django.conf import settings
from django.contrib.auth.models import User
from django.core.urlresolvers import reverse
from django.utils.translation import ugettext as _

from fun.utils.mongo import connect_to_mongo

logger = logging.getLogger(__name__)

MONGO = settings.CONTENTSTORE['DOC_STORE_CONFIG']
COLLECTION = 'cached_api_proctoru'

API_URLS = {
    "get_time_zone": "/api/getTimeZoneList",
    "get_sche_info_avl_time_list": "/api/getScheduleInfoAvailableTimesList",
    "add_adhoc_process": "/api/addAdHocProcess",
    "remove_reservation": "/api/removeReservation",
    "client_activity_report": "/api/clientActivityReport",
    "student_reservation_list": "/api/getStudentReservationList",
    "begin_reservation": "/api/beginReservation",
    "edit_student": "/api/editStudent",
}

BASE_URL = "https://" + settings.PROCTORU_API
HEADER = {"Authorization-Token": settings.PROCTORU_TOKEN}


def get_proctoru_api_collection(drop=False):
    db = connect_to_mongo()
    if drop:
        db.connection[MONGO['db']].drop_collection(COLLECTION)
    else:
        db.connection[MONGO['db']]

    return db[COLLECTION]


def get_mongo_reports(course_key):
    """
    Query mongo to get the cached version of PU API
    Args:
        course_key: the course_key_object for the wanted course

    Returns: a dict ("data": dict, "last_update": datetime)
     * dict : the aggregated reports for verified users, with student grades
     * datetime : the time the cache was updated last

    Error returns : dict ("data": the reason of the error, "last_update": None or the datetime object of the last mongo update)
     * When we can't get the info in mongo
     * error created by filter_reports_for_course or aggregate_reports_per_user
    """
    cached_api_proctoru = get_proctoru_api_collection()

    mongo_datas = cached_api_proctoru.find_one()
    if not mongo_datas:
        pb = {"id": "Mongo database is empty, come back later",
              "start": None,
              "end": None}
        return {"data": {"warn": pb}, "last_update": None}

    student_activities = mongo_datas["reports"]
    try:
        student_grades = mongo_datas["grades"][unicode(course_key)]
    except KeyError:
        pb = {"id": "Course {} not found".format(unicode(course_key)),
                         "start": None,
                         "end": None}
        return {"data": {"warn": pb}, "last_update": None}

    last_updated = mongo_datas["last_update"]

    filtered_reports = filter_reports_for_course(course_key.course, course_key.run, None, None, student_activities)
    if {"warn", "error"}.intersection(filtered_reports.keys()) :  # something went wrong
        return {"data":filtered_reports, "last_update":last_updated}

    reports_aggregated = aggregate_reports_per_user(filtered_reports["results"], student_grades)
    return {"data": reports_aggregated, "last_update": last_updated}


def split_large_date_range(start_date, end_date, increment):
    """
    Split a date range in multiple time intervals of increment days

    :param start_date: start of the interval
    :param end_date: end of the interval
    :param increment: value to increment (int)
    :yield: tuples with sub interval start and end [(start_date, int1), (int1, int2), ... , (int_n, end_date)]
    """
    cur = start_date
    delta = relativedelta.relativedelta(days=increment)
    while cur + delta < end_date:
        yield cur, cur + delta
        cur += delta
    if cur != end_date:
        yield cur, end_date

def query_api(request_method, url, data):
    data["time_sent"] = datetime.datetime.utcnow().isoformat()

    logger.info("sending request to proctoru API, url: {} - header:{} - data:{}".format(url, HEADER, data))
    try:
        resp = request_method(url, data=data, headers=HEADER)
    except requests.ConnectionError as e:
        logger.exception(e)
        return {"error": "Connection error while connecting to {}".format(url)}

    if resp.status_code == 500:
        mess = "Proctoru resp: {} -- data {}".format(resp.content, data)
        logger.error(mess)
        return {"error": mess}

    student_activity = resp.content
    student_activity_json = json.loads(student_activity)
    if student_activity_json["response_code"] != 1:
        if student_activity_json["message"] == "stale request":
            logger.error("Error in ProctorU API configuration, received : stale request -- data: {}".format(data))
            return {"error": student_activity_json["message"]}
        else:
            logger.error("ProctorU API error, message : {} -- data: {}".format(student_activity_json["message"], data))
            return {"error": student_activity_json["message"]}
    return student_activity_json


def is_in_prod():
    """
    Utility function to tell in we are in test, dev or preprod/prod settings.
    We don't query the users the same way according the setup.

    :return: boolean are we in preprod/prod or not
    """
    try:
        settings.TEST_ROOT  # are we in test mode ?
    except AttributeError:
        test = False
    else:
        test = True

    if test:
        return False  # if we are in test, we are not in prod

    return not settings.DEBUG  # if we are in debug mode, we are not in prod


def extract_infos(report):
    tmp = {
        "Student": report["Student"],
        "UniqueId": report["UniqueId"],
        "ProctorNotes": "",
        "ReservationNo": report["ReservationNo"],
        "TestSubmitted": report["Authenticated"],
        "CheckID": report["CheckID"],
        "StartDate": format_date(report["StartDate"]),
        "Authenticated": report["Authenticated"],
        "IncidentReport": report["IncidentReport"],
        "Escalated": report["Escalated"],
        "fun_user_url": None,
        "fun_exam_grade": None,
        "fun_exam_pass": None,
    }

    if report["ProctorNotes"] :
        stripped = report["ProctorNotes"].strip()
        tmp["ProctorNotes"] = [remark.strip() for remark in stripped.split(";") if remark]

    return tmp


def get_reports_from_ids(course_name, course_run, student_ids):
    """ Query proctoru API from a list of student ids
    :param course_name: name of the course (for proctoru identification)
    :param course_run: run of the course (for proctoru identification)
    :param student_ids: list of student ids enrolled in verified mode with proctoru reservation
    :return: the aggregated reports for course per user
    """
    student_activities = []
    logger.info("starting proctoru query from student ids: {}".format(student_ids))

    for student_id in student_ids:
        student_activity = query_api(requests.post,
                                     BASE_URL + API_URLS["client_activity_report"],
                                     request_infos(None, None, student_pk=student_id))
        if "error" in student_activity:
            if "student not found" not in student_activity["error"].lower():
                # if student is not found, we continue, else we return
                return student_activity
        else:
            student_activities.append(student_activity)

    filtered_reports = filter_reports_for_course(course_name, course_run, None, None, student_activities)
    if {"warn", "error"}.intersection(filtered_reports.keys()) :  # something went wrong
        return filtered_reports

    return aggregate_reports_per_user(filtered_reports["results"])


def get_reports_from_interval(course_name, course_run, request_start_date, request_end_date=None):
    """ Query proctoru API from a start_date and an end_date
    :param course_name: string with the name of the course (course.id.name)
    :param course_run: string with the run of the course (course.id.run)
    :param request_start_date: datetime object, specify the start of the interval
    :param request_end_date: datetime object, specify the end of the interval
    :return: dict {user: [report1, report2, ...]}
    """
    student_activities = []
    if not request_end_date:
        request_end_date = datetime.datetime.today()
    interval = 10

    logger.info("starting proctoru query from interval with begin: {} and end: {}".format(request_start_date, request_end_date))

    for start, end in split_large_date_range(request_start_date, request_end_date, interval):
        student_activity = query_api(requests.post,
                                     BASE_URL + API_URLS["client_activity_report"],
                                     request_infos(start, end))
        if "error" in student_activity:
            return student_activity

        student_activities.append(student_activity)

    filtered_reports = filter_reports_for_course(course_name, course_run, request_start_date, request_end_date, student_activities)
    if {"warn", "error"}.intersection(filtered_reports.keys()) :  # something went wrong
        return filtered_reports

    return aggregate_reports_per_user(filtered_reports["results"])


def aggregate_reports_per_user(filtered_reports, student_grades={}):
    """
    Aggregate the lines present in the API according the user.
    The API returns a line for each events, but we are really interested in the "profile" for each user.

    ProctorU API seems bugged, it contains duplicated lines (usually consecutive), so we also remove the consecutive
    duplicates.

    A user can have mutliple reservations, for instance when he didn't present to the exam or when there are some
    connection issues with the proctor. A reservation contains one or multiple proctorNotes
    We want to sort the reports according their reservation number, in a descending way (the most recent is the first).

    :param filtered_reports: iterable with the ProctorU reports filtered with the course of interest
    :param student_grades: dictionary with the student username as key and info about their exam grade in values
    :return: dict with the fun username as key and the list of "actions" / reports for this user
    """
    student_grades = student_grades or {}

    filtered_reports.sort(key=lambda d: d["Student"])

    first_report = filtered_reports[0]
    id_ = int(first_report["UniqueId"])
    identifiers = [id_]
    event_users = defaultdict(list)
    event_users[id_].append(extract_infos(first_report))

    prec_json = filtered_reports[0]
    for report in filtered_reports[1:]:
        tmp = extract_infos(report)
        if not("UniqueId" in report and report["UniqueId"]):
            prec_json = report
            logger.error("PU report without UniqueId: {}".format(report))
            continue

        id_ = int(report["UniqueId"])
        if report != prec_json:
            event_users[id_].append(tmp)
            identifiers.append(id_)
        prec_json = report

    event_users = dict(event_users)
    if not is_in_prod():
        fun_users = User.objects.filter(last_name__in=identifiers)
    else:
        fun_users = User.objects.filter(id__in=identifiers)

    for user in event_users:
        event_users[user].sort(key=lambda x: -x["ReservationNo"])

    res = {}
    for user in fun_users:
        user_info = {}
        if not is_in_prod():
            id_ = int(user.last_name)  # Ugly! : in dev we added the production primary key in the filed last name
        else:
            id_ = user.id

        url = reverse("backoffice:user-detail", args=[user.username])
        user_info["fun_user_url"] = url

        if user.username in student_grades:
            user_info["fun_exam_grade"] = student_grades[user.username]["grade"]
            user_info["fun_exam_pass"] = student_grades[user.username]["passed"]

        event_users[id_][0].update(user_info)
        res[user.username] = event_users[id_]

    return res


def users_with_cancelled_reservations(aggregated_reports):
    usernames = []
    for username in aggregated_reports:
        last_reports = aggregated_reports[username][0]["ProctorNotes"]
        last_remark = last_reports[0]

        if "cancelled" in last_remark:
            usernames.append(username)

    students = User.objects.filter(username__in=usernames)
    return students


def filter_reports_for_course(course_name, course_run, start_date, end_date, student_activities):
    """
    Only keep the course of interest from the API query.
    This should be done API side, but it is not possible for the moment :(

    :param course_name: str course ID
    :param course_run: str session ID
    :param start_date: datetime object with the starting date of the query (for the logs)
    :param end_date: datetime object with the ending date of the query (for the logs)
    :param student_activities: list of dicts with the API response
    :return: dict with the API response about the course
    """
    exam_id = "{} {}".format(course_name, course_run)
    reports = []
    for student_activity in student_activities:
        if "data" in student_activity and student_activity["data"]:
            reports += student_activity['data']

    if not reports:
        mess = "Empty response from the API"
        logger.info(mess)
        return {"error": mess}

    filtered_reports = [report for report in reports if exam_id in report["Test"]]  # "Test" is the name of the exam in proctoru API
    if not filtered_reports:
        mess = "No student for course {} between {} and {}".format(exam_id, start_date, end_date)
        logger.info(mess)
        return {"warn": {"id": exam_id,
                         "start": format_date(str(start_date)),
                         "end": format_date(str(end_date))}}

    return {"results": filtered_reports}


def format_date(date_str):
    """ Tries to translate and format a ISO date
    :param date_str: raw string
    :return: the translated date or None
    """
    try:
        return dateutil.parser.parse(date_str).strftime(_('%m/%d/%y %H:%M'))
    except ValueError:  # sometimes we got no dates in string
        return None

def request_infos(begin, end, student_pk=None):
    """
    Creates the dic with correctly formatted information
    :param begin: the date of the beginning of the request
    :param end: the date of the end of the request
    :param student_pk: the "proctoru student unique ID" (ie : primary key for us) of a student
    :return: a dictionary properly configured
    """
    data = {}

    if begin:
        data["start_date"] = begin.isoformat()
    if end:
        data["end_date"] = end.isoformat()
    if student_pk:
        data["student_id"] = student_pk

    return data


def is_proctoru_ok(proctoru_student_reports):
    """Check ProctorU accept conditions from the reports

    As a debug feature, this function will return True for all students when
    the PROCTORU_ALL_STUDENTS_OK environment variable is defined. This is a
    temporary measure that should be removed when we don't need it anymore.
    (i.e: when we have found a way to abstract ourselves from the ProctorU API)
    """
    if os.environ.get("PROCTORU_ALL_STUDENTS_OK"):
        return True

    for report in proctoru_student_reports:
        ok = report.get("Authenticated", False) and report.get("TestSubmitted", False) and not report.get("Escalated", False) and not report.get("IncidentReport", False)
        if ok:
            return True
    return False


def students_registered_in_pu(reports):
    usernames = reports.keys()
    students = User.objects.filter(username__in=usernames)
    return students