# -*- coding: utf-8 -*-
from django.db import models

from fun.utils.managers import ChainableManager


class UniversityQuerySet(models.query.QuerySet):

    def not_obsolete(self):
        return self.filter(is_obsolete=False)

    def detail_page_enabled(self):
        return self.filter(detail_page_enabled=True)

    def with_related(self):
        queryset = self.prefetch_related('courses')
        return queryset

    def by_score(self):
        return self.order_by('-score', 'name')

    def has_external_link(self):
        return self.exclude(site_url='')

    def has_details_link(self):
        return self.has_external_link() | self.detail_page_enabled()

    def has_icon_visible(self):
        return self.not_obsolete().has_details_link()

    def featured(self, limit_to=None):
        """Featured universities have a detail page and are not obsolete"""
        qs = self.has_icon_visible().with_related().by_score()
        if limit_to is not None:
            qs = qs[:limit_to]
        return qs
