#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2009 Zuza Software Foundation
# Copyright 2013 Evernote Corporation
#
# This file is part of Pootle.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, see <http://www.gnu.org/licenses/>.

import os
os.environ['DJANGO_SETTINGS_MODULE'] = 'pootle.settings'

import logging

from optparse import make_option

from django.conf import settings
from django.core.cache import cache
from django.core.urlresolvers import set_script_prefix
from django.db.models import Count, Max, Sum
from django.utils import dateformat
from django.utils.encoding import iri_to_uri

from pootle_language.models import Language
from pootle_misc.util import datetime_min
from pootle_project.models import Project
from pootle_statistics.models import Submission
from pootle_store.models import Store, Unit, QualityCheck, Suggestion
from pootle_store.util import OBSOLETE, UNTRANSLATED, FUZZY, TRANSLATED

from . import PootleCommand


class Command(PootleCommand):
    help = "Allow stats and text indices to be refreshed manually."

    shared_option_list = (
        make_option('--calculate-checks', dest='calculate_checks',
                    action='store_true',
                    help='To recalculate quality checks for all strings'),
        )
    option_list = PootleCommand.option_list + shared_option_list

    def handle_noargs(self, **options):
        # set url prefix
        set_script_prefix(settings.FORCE_SCRIPT_NAME)
        super(Command, self).handle_noargs(**options)

    def handle_all_stores(self, translation_project, **options):
        # TODO use the faster method
        translation_project.flush_cache()
        translation_project.get_stats()
        translation_project.get_mtime()

    def handle_store(self, store, **options):
        # TODO use the faster method
        store.flush_cache()
        store.get_stats()
        store.get_mtime()

    def handle_language(self, lang, **options):
        # TODO use the faster method
        lang.flush_cache(False)
        lang.get_stats()
        lang.get_mtime()

    def handle_project(self, prj, **options):
        # TODO use the faster method
        prj.flush_cache(False)
        prj.get_stats()
        prj.get_mtime()

    def handle_all(self, **options):
        timeout = settings.OBJECT_CACHE_TIMEOUT
        calculate_checks = options.get('calculate_checks', False)

        logging.info('Initializing stores...')
        self._init_stores()

        if calculate_checks:
            logging.info('Calculating quality checks for all units...')
            QualityCheck.objects.all().delete()

            for store in Store.objects.iterator():
                logging.info("update_qualitychecks %s" % store.pootle_path)
                for unit in store.units.iterator():
                    unit.update_qualitychecks(created=True)

        logging.info('Setting quality check stats values for all stores...')
        self._set_qualitycheck_stats(timeout)
        logging.info('Setting last action values for all stores...')
        self._set_last_action_stats(timeout)
        logging.info('Setting last updated values for all stores...')
        self._set_last_updated_stats(timeout)
        logging.info('Setting mtime values for all stores...')
        self._set_mtime_stats(timeout)
        logging.info('Setting wordcount stats values for all stores...')
        self._set_wordcount_stats(timeout)
        logging.info('Setting suggestion count values for all stores...')
        self._set_suggestion_stats(timeout)

        logging.info('Setting empty values for other cache entries...')
        self._set_empty_values(timeout)

        logging.info('Refreshing directories stats...')

        lang_query = Language.objects.all()
        prj_query = Project.objects.all()

        for lang in lang_query.iterator():
            # Calculate stats for all directories and translation projects
            lang.refresh_stats()

        for prj in prj_query.iterator():
            prj.refresh_stats(False)

    def _set_qualitycheck_stats_cache(self, stats, key, timeout):
        if key:
            logging.info('Set get_checks for %s' % key)
            cache.set(iri_to_uri(key + ':get_checks'), stats, timeout)
            del self.cache_values[key]['get_checks']

    def _set_qualitycheck_stats(self, timeout):
        queryset = QualityCheck.objects.filter(unit__state__gt=UNTRANSLATED,
                                               false_positive=False) \
                                       .values('unit', 'unit__store', 'name') \
                                       .order_by('unit__store', 'unit')

        saved_store = None
        saved_unit = None
        stats = None

        for item in queryset.iterator():
            if item['unit__store'] != saved_store:
                key = Store.objects.get(id=item['unit__store']).get_cachekey()
                saved_store = item['unit__store']
                stats = self.cache_values[key]['get_checks']['checks']

            if item['name'] in stats['checks']:
                stats['checks'][item['name']] += 1
            else:
                stats['checks'][item['name']] = 1

            if saved_unit != item['unit']:
                saved_unit = item['unit']
                stats['unit_count'] += 1

        for key in self.cache_values:
            stats = self.cache_values['get_checks']
            if stats['unit_count'] > 0:
                self._set_qualitycheck_stats_cache(stats, key, timeout)

    def _set_wordcount_stats_cache(self, stats, key, timeout):
        if key:
            logging.info('Set wordcount stats for %s' % key)
            cache.set(iri_to_uri(key + ':get_total_wordcount'),
                      stats['total'], timeout)
            cache.set(iri_to_uri(key + ':get_fuzzy_wordcount'),
                      stats[FUZZY], timeout)
            cache.set(iri_to_uri(key + ':get_translated_wordcount'),
                      stats[TRANSLATED], timeout)
            del self.cache_values[key]['get_total_wordcount']
            del self.cache_values[key]['get_fuzzy_wordcount']
            del self.cache_values[key]['get_translated_wordcount']

    def _set_wordcount_stats(self, timeout):
        res = Unit.objects.filter(state__gt=OBSOLETE) \
                          .values('store', 'state') \
                          .annotate(wordcount=Sum('source_wordcount')) \
                          .order_by('store', 'state')

        saved_id = None
        saved_key = None
        stats = None
        key = None

        for item in res.iterator():
            if saved_id != item['store']:
                key = Store.objects.get(id=item['store']).get_cachekey()
                if saved_key:
                    self._set_wordcount_stats_cache(stats, saved_key, timeout)

                stats = {'total': 0, FUZZY: 0, TRANSLATED: 0}
                saved_key = key
                saved_id = item['store']

            stats['total'] += item['wordcount']

            if item['state'] in [FUZZY, TRANSLATED]:
                stats[item['state']] = item['wordcount']

        if saved_id:
            self._set_wordcount_stats_cache(stats, key, timeout)

    def _init_stores(self):
        self.cache_values = {}
        for store in Store.objects.all():
            self.cache_values[store.get_cachekey()] = {
                'get_last_action': {'id': 0, 'mtime': 0, 'snippet': ''},
                'get_suggestion_count': 0,
                'get_checks': {'unit_count': 0, 'checks': {}},
                'get_total_wordcount': 0,
                'get_translated_wordcount': 0,
                'get_fuzzy_wordcount': 0,
                'get_mtime': datetime_min,
                'get_last_updated': {'id': 0, 'creation_time': 0,
                                     'snippet': ''},
            }

    def _set_empty_values(self, timeout):
        for key, value in self.cache_values.items():
            for func in value.keys():
                cache.set(iri_to_uri(key + ':' + func), value[func], timeout)

    def _set_last_action_stats(self, timeout):
        ss = Submission.simple_objects.values('unit__store__pootle_path') \
                                      .annotate(max_id=Max('id'))
        for s_id in ss.iterator():
            sub = Submission.objects.select_related('unit__store') \
                                    .get(id=s_id['max_id'])
            if sub.unit:
                key = sub.unit.store.get_cachekey()
                logging.info('Set last action stats for %s' % key)
                res = {
                    'id': sub.unit.id,
                    'mtime': int(dateformat.format(sub.creation_time, 'U')),
                    'snippet': sub.get_submission_message()
                }
                cache.set(iri_to_uri(key + ':get_last_action'), res, timeout)
                del self.cache_values[key]['get_last_action']

    def _set_suggestion_stats(self, timeout):
        queryset = Suggestion.objects.filter(unit__state__gt=OBSOLETE) \
                                     .values('unit__store') \
                                     .annotate(count=Count('id'))

        for item in queryset.iterator():
            key = Store.objects.get(id=item['unit__store']).get_cachekey()
            logging.info('Set suggestion count for %s' % key)
            cache.set(iri_to_uri(key + ':get_suggestion_count'),
                      item['count'], timeout)
            del self.cache_values[key]['get_suggestion_count']

    def _set_mtime_stats(self, timeout):
        queryset = Unit.objects.values('store').annotate(
            max_mtime=Max('mtime')
        )

        for item in queryset.iterator():
            key = Store.objects.get(id=item['store']).get_cachekey()
            logging.info('Set mtime for %s' % key)
            cache.set(iri_to_uri(key + ':get_mtime'),
                      item['max_mtime'], timeout)
            del self.cache_values[key]['get_mtime']

    def _set_last_updated_stats(self, timeout):
        queryset = Unit.objects.values('store').annotate(
            max_creation_time=Max('creation_time')
        )

        for item in queryset.iterator():
            max_time = item['max_creation_time']
            if max_time:
                store = Store.objects.get(id=item['store'])
                unit = store.unit_set.filter(creation_time=max_time)[0]
                key = store.get_cachekey()
                logging.info('Set last_updated for %s' % key)
                res = {
                    'id': unit.id,
                    'creation_time': int(dateformat.format(max_time, 'U')),
                    'snippet': unit.get_last_updated_message()
                }
                cache.set(iri_to_uri(key + ':get_last_updated'), res, timeout)
                del self.cache_values[key]['get_last_updated']
