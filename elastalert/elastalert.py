# -*- coding: utf-8 -*-
import copy
import datetime
import json
import logging
import os
import sys
import time
import traceback

import argparse

import kibana
from alerts import DebugAlerter
from config import load_rules
from config import get_rule_hashes
from config import load_configuration
from util import dt_to_ts
from util import EAException
from util import pretty_ts
from util import ts_add
from util import ts_delta
from util import ts_now
from util import ts_to_dt
from util import format_index
from elasticsearch.client import Elasticsearch
from elasticsearch.exceptions import ElasticsearchException


class ElastAlerter():
    """ The main Elastalert runner. This class holds all state about active rules,
    controls when queries are run, and passes information between rules and alerts.

    :param args: An argparse arguments instance. Should contain debug and start

    :param conf: The configuration dictionary. At the top level, this
    contains global options, and under 'rules', contains all state relating
    to rules and alerts. In each rule in conf['rules'], the RuleType and Alerter
    instances live under 'type' and 'alerts', respectively. The conf dictionary
    should not be passed directly from a configuration file, but must be populated
    by config.py:load_rules instead. """

    def parse_args(self, args):
        parser = argparse.ArgumentParser()
        parser.add_argument('--config', action='store', dest='config', default="config.yaml", help='Global config file (default: config.yaml)')
        parser.add_argument('--debug', action='store_true', dest='debug', help='Suppresses alerts and prints information instead')
        parser.add_argument('--rule', dest='rule', help='Run only a specific rule (by filename, must still be in rules folder)')
        parser.add_argument('--silence', dest='silence', help='Silence rule for a time period. Must be used with --rule. Usage: '
                                                              '--silence <units>=<number>, eg. --silence hours=2')
        parser.add_argument('--start', dest='start', help='YYYY-MM-DDTHH:MM:SS Start querying from this timestamp. (Default: present)')
        parser.add_argument('--end', dest='end', help='YYYY-MM-DDTHH:MM:SS Query to this timestamp. (Default: present)')
        parser.add_argument('--verbose', action='store_true', dest='verbose', help='Increase verbosity without suppressing alerts')
        parser.add_argument('--pin_rules', action='store_true', dest='pin_rules', help='Stop ElastAlert from monitoring config file changes')
        self.args = parser.parse_args(args)

    def __init__(self, args):
        self.parse_args(args)
        self.conf = load_rules(self.args.config, use_rule=self.args.rule)
        self.max_query_size = self.conf['max_query_size']
        self.rules = self.conf['rules']
        self.debug = self.args.debug
        self.verbose = self.args.verbose
        self.writeback_index = self.conf['writeback_index']
        self.es_host = self.conf['es_host']
        self.es_port = self.conf['es_port']
        self.run_every = self.conf['run_every']
        self.alert_time_limit = self.conf['alert_time_limit']
        self.old_query_limit = self.conf['old_query_limit']
        self.alerts_sent = 0
        self.num_hits = 0
        self.current_es = None
        self.current_es_addr = None
        self.buffer_time = self.conf['buffer_time']
        self.silence_cache = {}
        self.rule_hashes = get_rule_hashes(self.conf)

        self.writeback_es = Elasticsearch(host=self.es_host, port=self.es_port)

        if self.debug:
            self.verbose = True

        if self.verbose:
            logging.getLogger().setLevel(logging.INFO)

        for rule in self.rules:
            rule = self.init_rule(rule)

        if self.args.silence:
            self.silence()

    def get_index(self, rule, starttime=None, endtime=None):
        """ Gets the index for a rule. If strftime is set and starttime and endtime
        are provided, it will return a comma seperated list of indices. If strftime
        is set but starttime and endtime are not provided, it will replace all format
        tokens with a wildcard. """
        index = rule['index']
        if rule.get('use_strftime_index'):
            if starttime and endtime:
                return format_index(index, starttime, endtime)
            else:
                # Replace the substring containing format characters with a *
                format_start = index.find('%')
                format_end = index.rfind('%') + 2
                return index[:format_start] + '*' + index[format_end:]
        else:
            return index

    @staticmethod
    def get_query(filters, starttime=None, endtime=None, sort=True, timestamp_field='@timestamp'):
        """ Returns a query dict that will apply a list of filters, filter by
        start and end time, and sort results by timestamp.

        :param filters: A list of elasticsearch filters to use.
        :param starttime: A timestamp to use as the start time of the query.
        :param endtime: A timestamp to use as the end time of the query.
        :param sort: If true, sort results by timestamp. (Default True)
        :return: A query dictionary to pass to elasticsearch.
        """
        filters = copy.copy(filters)
        query = {'filter': {'bool': {'must': filters}}}
        if starttime and endtime:
            query['filter']['bool']['must'].append({'range': {timestamp_field: {'from': starttime,
                                                                                'to': endtime}}})
        if sort:
            query['sort'] = [{timestamp_field: {'order': 'asc'}}]
        return query

    def get_terms_query(self, query, size, field):
        """ Takes a query generated by get_query and outputs a aggregation query """
        if 'sort' in query:
            query.pop('sort')
        query.update({'aggs': {'counts': {'terms': {'field': field, 'size': size}}}})
        aggs_query = {'aggs': {'filtered': query}}
        return aggs_query

    def get_index_start(self, index, timestamp_field='@timestamp'):
        """ Query for one result sorted by timestamp to find the beginning of the index.

        :param index: The index of which to find the earliest event.
        :return: Timestamp of the earliest event.
        """
        query = {'sort': {timestamp_field: {'order': 'asc'}}}
        try:
            res = self.current_es.search(index=index, size=1, body=query, _source_include=[timestamp_field])
        except ElasticsearchException as e:
            self.handle_error("Elasticsearch query error: %s" % (e), {'index': index})
            return '1969-12-30T00:00:00Z'
        if len(res['hits']['hits']) == 0:
            # Index is completely empty, return a date before the epoch
            return '1969-12-30T00:00:00Z'
        timestamp = res['hits']['hits'][0]['_source'][timestamp_field]
        return timestamp

    def get_hits(self, rule, starttime, endtime, index):
        """ Query elasticsearch for the given rule and return the results.

        :param rule: The rule configuration.
        :param starttime: The earliest time to query.
        :param endtime: The latest time to query.
        :return: A list of hits, bounded by self.max_query_size.
        """
        query = self.get_query(rule['filter'], starttime, endtime, timestamp_field=rule['timestamp_field'])
        try:
            res = self.current_es.search(index=index, size=self.max_query_size, body=query, _source_include=rule['include'])
        except ElasticsearchException as e:
            # Elasticsearch sometimes gives us GIGANTIC error messages
            # (so big that they will fill the entire terminal buffer)
            if len(str(e)) > 1024:
                e = str(e)[:1024] + '... (%d characters removed)' % (len(str(e)) - 1024)
            self.handle_error('Error running query: %s' % (e), {'rule': rule['name']})
            return None

        hits = res['hits']['hits']
        self.num_hits += len(hits)
        lt = rule.get('use_local_time')
        logging.info("Queried rule %s from %s to %s: %s hits" % (rule['name'], pretty_ts(starttime, lt), pretty_ts(endtime, lt), len(hits)))

        return hits

    def get_hits_count(self, rule, starttime, endtime, index):
        """ Query elasticsearch for the count of results and returns a list of timestamps
        equal to the endtime. This allows the results to be passed to rules which expect
        an object for each hit.

        :param rule: The rule configuration dictionary.
        :param starttime: The earliest time to query.
        :param endtime: The latest time to query.
        :return: A dictionary mapping timestamps to number of hits for that time period.
        """
        query = self.get_query(rule['filter'], starttime, endtime, timestamp_field=rule['timestamp_field'], sort=False)
        query = {'query': {'filtered': query}}

        try:
            res = self.current_es.count(index=index, doc_type=rule['doc_type'], body=query)
        except ElasticsearchException as e:
            # Elasticsearch sometimes gives us GIGANTIC error messages
            # (so big that they will fill the entire terminal buffer)
            if len(str(e)) > 1024:
                e = str(e)[:1024] + '... (%d characters removed)' % (len(str(e)) - 1024)
            self.handle_error('Error running count query: %s' % (e), {'rule': rule['name']})
            return None

        self.num_hits += res['count']
        lt = rule.get('use_local_time')
        logging.info("Queried rule %s from %s to %s: %s hits" % (rule['name'], pretty_ts(starttime, lt), pretty_ts(endtime, lt), res['count']))
        return {endtime: res['count']}

    def get_hits_terms(self, rule, starttime, endtime, index):
        base_query = self.get_query(rule['filter'], starttime, endtime, timestamp_field=rule['timestamp_field'], sort=False)
        query = self.get_terms_query(base_query, rule['terms_size'], rule['query_key'])

        try:
            res = self.current_es.search(index=index, doc_type=rule['doc_type'], body=query, search_type='count')
        except ElasticsearchException as e:
            # Elasticsearch sometimes gives us GIGANTIC error messages
            # (so big that they will fill the entire terminal buffer)
            if len(str(e)) > 1024:
                e = str(e)[:1024] + '... (%d characters removed)' % (len(str(e)) - 1024)
            self.handle_error('Error running query: %s' % (e), {'rule': rule['name']})
            return None

        buckets = res['aggregations']['filtered']['counts']['buckets']
        self.num_hits += len(buckets)
        lt = rule.get('use_local_time')
        logging.info('Queried rule %s from %s to %s: %s buckets' % (rule['name'], pretty_ts(starttime, lt), pretty_ts(endtime, lt), len(buckets)))
        return {endtime: buckets}

    def remove_duplicate_events(self, data, rule):
        # Remove data we've processed already
        data = [event for event in data if event['_id'] not in rule['processed_hits']]

        # Remember the new data's IDs
        for event in data:
            rule['processed_hits'][event['_id']] = event['_source'][rule['timestamp_field']]

        return [event['_source'] for event in data]

    def remove_old_events(self, rule):
        # Anything older than the buffer time we can forget
        now = ts_now()
        remove = []
        buffer_time = rule.get('buffer_time', self.buffer_time)
        for _id, timestamp in rule['processed_hits'].iteritems():
            if ts_delta(timestamp, now) > buffer_time:
                remove.append(_id)
        map(rule['processed_hits'].pop, remove)

    def run_query(self, rule, start=None, end=None):
        """ Query for the rule and pass all of the results to the RuleType instance.

        :param rule: The rule configuration.
        :param start: The earliest time to query.
        :param end: The latest time to query.
        Returns True on success and False on failure.
        """
        if start is None:
            start = self.get_index_start(rule['index'])
        if end is None:
            end = ts_now()

        # Reset hit counter and query
        rule_inst = rule['type']
        prev_num_hits = self.num_hits
        max_size = rule.get('max_query_size', self.max_query_size)
        index = self.get_index(rule, start, end)
        if rule.get('use_count_query'):
            data = self.get_hits_count(rule, start, end, index)
        elif rule.get('use_terms_query'):
            data = self.get_hits_terms(rule, start, end, index)
        else:
            data = self.get_hits(rule, start, end, index)
            if data:
                data = self.remove_duplicate_events(data, rule)

        # There was an exception while querying
        if data is None:
            return False
        elif data:
            if rule.get('use_count_query'):
                rule_inst.add_count_data(data)
            elif rule.get('use_terms_query'):
                rule_inst.add_terms_data(data)
            else:
                rule_inst.add_data(data)

        # Warn if we hit max_query_size
        if self.num_hits - prev_num_hits == max_size and not rule.get('use_count_query'):
            logging.warning("Hit max_query_size (%s) while querying for %s" % (max_size, rule['name']))

        return True

    def get_starttime(self, rule):
        """ Query ES for the last time we ran this rule.

        :param rule: The rule configuration.
        :return: A timestamp or None.
        """
        query = {'filter': {'term': {'rule_name': '%s' % (rule['name'])}},
                 'sort': {'@timestamp': {'order': 'desc'}}}
        try:
            if self.writeback_es:
                res = self.writeback_es.search(index=self.writeback_index, doc_type='elastalert_status',
                                               size=1, body=query, _source_include=['endtime', 'rule_name'])
                if res['hits']['hits']:
                    endtime = res['hits']['hits'][0]['_source']['endtime']

                    if ts_delta(endtime, ts_now()) < self.old_query_limit:
                        return endtime
                    else:
                        logging.info("Found expired previous run for %s at %s" % (rule['name'], endtime))
                        return None
        except (ElasticsearchException, KeyError) as e:
            self.handle_error('Error querying for last run: %s' % (e), {'rule': rule['name']})
            self.writeback_es = None

        return None

    def set_starttime(self, rule, endtime):
        """ Given a rule and an endtime, sets the appropriate starttime for it. """

        # This means we are starting fresh
        if 'starttime' not in rule:
            # Try to get the last run from elasticsearch
            last_run_end = self.get_starttime(rule)
            if last_run_end:
                rule['starttime'] = last_run_end
                return

        # Use buffer for normal queries, or run_every increments otherwise
        buffer_time = rule.get('buffer_time', self.buffer_time)
        if not rule.get('use_count_query') and not rule.get('use_terms_query'):
            rule['starttime'] = ts_add(endtime, -buffer_time)
        else:
            rule['starttime'] = ts_add(endtime, -self.run_every)

    def run_rule(self, rule, endtime, starttime=None):
        """ Run a rule for a given time period, including querying and alerting on results.

        :param rule: The rule configuration.
        :param starttime: The earliest timestamp to query.
        :param endtime: The latest timestamp to query.
        :return: The number of matches that the rule produced.
        """
        run_start = time.time()
        self.current_es = Elasticsearch(host=rule['es_host'], port=rule['es_port'])
        self.current_es_addr = (rule['es_host'], rule['es_port'])

        # If there are pending aggregate matches, try processing them
        for x in range(len(rule['agg_matches'])):
            match = rule['agg_matches'].pop()
            self.add_aggregated_alert(match, rule)

        # Start from provided time if it's given
        if starttime:
            rule['starttime'] = starttime
        else:
            self.set_starttime(rule, endtime)
        rule['original_starttime'] = rule['starttime']

        # Don't run if starttime was set to the future
        if ts_delta(rule['starttime'], ts_now()) <= datetime.timedelta(0):
            logging.warning("Attempted to use query start time in the future (%s), sleeping instead" % (starttime))
            return 0

        # Run the rule
        # If querying over a large time period, split it up into chunks
        self.num_hits = 0
        tmp_endtime = endtime
        buffer_time = rule.get('buffer_time', self.buffer_time)
        while ts_delta(rule['starttime'], endtime) > buffer_time:
            tmp_endtime = ts_add(rule['starttime'], self.run_every)
            if not self.run_query(rule, rule['starttime'], tmp_endtime):
                return 0
            rule['starttime'] = tmp_endtime
        if not self.run_query(rule, rule['starttime'], endtime):
            return 0

        rule['type'].garbage_collect(endtime)

        # Process any new matches
        num_matches = len(rule['type'].matches)
        while rule['type'].matches:
            match = rule['type'].matches.pop(0)

            # If realert is set, silence the rule for that duration
            # Silence is cached by query_key, if it exists
            # Default realert time is 0 seconds

            # concatenate query_key (or none) with rule_name to form silence_cache key
            if 'query_key' in rule:
                try:
                    key = '.' + match[rule['query_key']]
                except KeyError:
                    # Some matches may not have a query key
                    key = ''
            else:
                key = ''

            if self.is_silenced(rule['name'] + key) or self.is_silenced(rule['name']):
                logging.info('Ignoring match for silenced rule %s%s' % (rule['name'], key))
                continue

            if rule['realert']:
                self.set_realert(rule['name'] + key, dt_to_ts(datetime.datetime.utcnow() + rule['realert']))

            # If no aggregation, alert immediately
            if not rule['aggregation']:
                self.alert([match], rule)
                continue

            # Add it as an aggregated match
            self.add_aggregated_alert(match, rule)

        time_taken = time.time() - run_start
        # Write to ES that we've run this rule against this time period
        body = {'rule_name': rule['name'],
                'endtime': endtime,
                'starttime': rule['starttime'],
                'matches': num_matches,
                'hits': self.num_hits,
                '@timestamp': ts_now(),
                'time_taken': time_taken}
        self.writeback('elastalert_status', body)

        return num_matches

    def init_rule(self, new_rule, new=True):
        ''' Copies some necessary non-config state from an exiting rule to a new rule. '''
        if 'download_dashboard' in new_rule['filter']:
            # Download filters from kibana and set the rules filters to them
            db_filters = self.filters_from_kibana(new_rule, new_rule['filter']['download_dashboard'])
            if db_filters is not None:
                new_rule['filter'] = db_filters
            else:
                raise EAException("Could not download filters from %s" % (new_rule['filter']['download_dashboard']))

        blank_rule = {'agg_matches': [],
                      'current_aggregate_id': None,
                      'processed_hits': {}}
        rule = blank_rule

        # Set rule to either a blank template or existing rule with same name
        if not new:
            for rule in self.rules:
                if rule['name'] == new_rule['name']:
                    break
            else:
                logging.warning("Couldn't find existing rule %s, starting from scratch" % (new_rule['name']))
                rule = blank_rule

        copy_properties = ['agg_matches',
                           'current_aggregate_id',
                           'processed_hits',
                           'starttime']
        for prop in copy_properties:
            if prop == 'starttime' and 'starttime' not in rule:
                continue
            new_rule[prop] = rule[prop]

        return new_rule

    def load_rule_changes(self):
        ''' Using the modification times of rule config files, syncs the running rules
        to match the files in rules_folder by removing, adding or reloading rules. '''
        rule_hashes = get_rule_hashes(self.conf)

        # Check each current rule for changes
        for rule_file, hash_value in self.rule_hashes.iteritems():
            if rule_file not in rule_hashes:
                # Rule file was deleted
                logging.info('Rule file %s not found, stopping rule execution' % (rule_file))
                self.rules = [rule for rule in self.rules if rule['rule_file'] != rule_file]
                continue
            if hash_value != rule_hashes[rule_file]:
                # Rule file was changed, reload rule
                try:
                    new_rule = load_configuration(os.path.join(self.conf['rules_folder'], rule_file))
                except EAException as e:
                    self.handle_error('Could not load rule %s: %s' % (rule_file, e))
                    continue
                logging.info("Reloading configuration for rule %s" % (rule_file))

                # Initialize the rule that matches rule_file
                self.rules = [rule if rule['rule_file'] != rule_file else self.init_rule(new_rule, False) for rule in self.rules]

        # Load new rules
        if not self.args.rule:
            for rule_file in set(rule_hashes.keys()) - set(self.rule_hashes.keys()):
                try:
                    new_rule = load_configuration(os.path.join(self.conf['rules_folder'], rule_file))
                except EAException as e:
                    self.handle_error('Could not load rule %s: %s' % (rule_file, e))
                    continue
                logging.info('Loaded new rule %s' % (rule_file))
                self.rules.append(self.init_rule(new_rule))

        self.rule_hashes = rule_hashes

    def start(self):
        """ Periodically go through each rule and run it """
        starttime = self.args.start
        if starttime:
            try:
                ts_to_dt(starttime)
            except (TypeError, ValueError):
                self.handle_error("%s is not a valid ISO 8601 timestamp (YYYY-MM-DDTHH:MM:SS+XX:00)" % (starttime))
                exit(1)
        while True:
            # If writeback_es errored, it's disabled until the next query cycle
            if not self.writeback_es:
                self.writeback_es = Elasticsearch(host=self.es_host, port=self.es_port)

            self.send_pending_alerts()

            next_run = datetime.datetime.utcnow() + self.run_every

            for rule in self.rules:
                # Set endtime based on the rule's delay
                delay = rule.get('query_delay')
                if hasattr(self.args, 'end') and self.args.end:
                    endtime = dt_to_ts(ts_to_dt(self.args.end))
                elif delay:
                    endtime = dt_to_ts(datetime.datetime.utcnow() - delay)
                else:
                    endtime = ts_now()

                try:
                    num_matches = self.run_rule(rule, endtime, starttime)
                except EAException as e:
                    self.handle_error("Error running rule %s: %s" % (rule['name'], e), {'rule': rule['name']})
                else:
                    old_starttime = pretty_ts(rule.get('original_starttime'), rule.get('use_local_time'))
                    logging.info("Ran %s from %s to %s: %s query hits, %s matches,"
                                 " %s alerts sent" % (rule['name'], old_starttime, pretty_ts(endtime, rule.get('use_local_time')),
                                                      self.num_hits, num_matches, self.alerts_sent))
                    self.alerts_sent = 0

                self.remove_old_events(rule)

            if next_run < datetime.datetime.utcnow():
                # We were processing for longer than our refresh interval
                # This can happen if --start was specified with a large time period
                # or if we are running too slow to process events in real time.
                logging.warning("Querying from %s to %s took longer than %s!" % (old_starttime, endtime, self.run_every))
                continue

            # Only force starttime once
            starttime = None

            if not self.args.pin_rules:
                self.load_rule_changes()

            # Wait before querying again
            sleep_for = (next_run - datetime.datetime.utcnow()).seconds
            logging.info("Sleeping for %s seconds" % (sleep_for))
            time.sleep(sleep_for)

    def generate_kibana_db(self, rule, match):
        ''' Uses a template dashboard to upload a temp dashboard showing the match.
        Returns the url to the dashboard. '''
        db = copy.deepcopy(kibana.dashboard_temp)

        # Set filters
        for filter in rule['filter']:
            if filter:
                kibana.add_filter(db, filter)
        kibana.set_included_fields(db, rule['include'])

        # Set index
        index = self.get_index(rule)
        kibana.set_index_name(db, index)

        return self.upload_dashboard(db, rule, match)

    def upload_dashboard(self, db, rule, match):
        ''' Uploads a dashboard schema to the kibana-int elasticsearch index associated with rule.
        Returns the url to the dashboard. '''
        # Set time range
        start = ts_add(match[rule['timestamp_field']], -rule.get('timeframe', datetime.timedelta(minutes=10)))
        end = ts_add(match[rule['timestamp_field']], datetime.timedelta(minutes=10))
        kibana.set_time(db, start, end)

        # Set dashboard name
        db_name = 'ElastAlert - %s - %s' % (rule['name'], end)
        kibana.set_name(db, db_name)

        # Add filter for query_key value
        if 'query_key' in rule:
            if rule['query_key'] in match:
                term = {'term': {rule['query_key']: match[rule['query_key']]}}
                kibana.add_filter(db, term)

        # Convert to json
        db_js = json.dumps(db)
        db_body = {'user': 'guest',
                   'group': 'guest',
                   'title': db_name,
                   'dashboard': db_js}

        # Upload
        es = Elasticsearch(host=rule['es_host'], port=rule['es_port'])
        res = es.create(index='kibana-int',
                        doc_type='temp',
                        body=db_body)

        # Return dashboard URL
        kibana_url = rule.get('kibana_dashboard')
        if not kibana_url:
            kibana_url = 'http://%s:%s/_plugin/kibana/' % (rule['es_host'],
                                                           rule['es_port'])
        return kibana_url + '#/dashboard/temp/%s' % (res['_id'])

    def get_dashboard(self, rule, db_name):
        """ Download dashboard which matches use_kibana_dashboard from elasticsearch. """
        es = Elasticsearch(host=rule['es_host'], port=rule['es_port'])
        if not db_name:
            raise EAException("use_kibana_dashboard undefined")
        query = {'query': {'term': {'_id': db_name}}}
        try:
            res = es.search(index='kibana-int', doc_type='dashboard', body=query, _source_include=['dashboard'])
        except ElasticsearchException as e:
            raise EAException("Error querying for dashboard: %s" % (e))

        if res['hits']['hits']:
            return json.loads(res['hits']['hits'][0]['_source']['dashboard'])
        else:
            raise EAException("Could not find dashboard named %s" % (db_name))

    def use_kibana_link(self, rule, match):
        """ Uploads an existing dashboard as a temp dashboard modified for match time.
        Returns the url to the dashboard. """
        # Download or get cached dashboard
        dashboard = rule.get('dashboard_schema')
        if not dashboard:
            db_name = rule.get('use_kibana_dashboard')
            dashboard = self.get_dashboard(rule, db_name)
        if dashboard:
            rule['dashboard_schema'] = dashboard
        else:
            return None
        dashboard = copy.deepcopy(dashboard)
        return self.upload_dashboard(dashboard, rule, match)

    def filters_from_kibana(self, rule, db_name):
        """ Downloads a dashboard from kibana and returns corresponding filters, None on error. """
        try:
            db = rule.get('dashboard_schema')
            if not db:
                db = self.get_dashboard(rule, db_name)
            filters = kibana.filters_from_dashboard(db)
        except EAException:
            return None
        return filters

    def alert(self, matches, rule, alert_time=None):
        """ Send out an alert.

        :param matches: A list of matches.
        :param rule: A rule configuration.
        """
        if alert_time is None:
            alert_time = ts_now()

        # Generate a kibana dashboard for the first match
        if rule.get('generate_kibana_link') or rule.get('use_kibana_dashboard'):
            try:
                if rule.get('generate_kibana_link'):
                    kb_link = self.generate_kibana_db(rule, matches[0])
                else:
                    kb_link = self.use_kibana_link(rule, matches[0])
            except EAException as e:
                self.handle_error("Could not generate kibana dash for %s match: %s" % (rule['name'], e))
            else:
                if kb_link:
                    matches[0]['kibana_link'] = kb_link

        for enhancement in rule['match_enhancements']:
            for match in matches:
                try:
                    enhancement.process(match)
                except EAException as e:
                    self.handle_error("Error running match enhancement: %s" % (e), {'rule': rule['name']})

        # Don't send real alerts in debug mode
        if self.debug:
            alerter = DebugAlerter(rule)
            alerter.alert(matches)
            return

        # Run the alerts
        alert_sent = False
        alert_exception = None
        for alert in rule['alert']:
            try:
                alert.alert(matches)
            except EAException as e:
                self.handle_error('Error while running alert %s: %s' % (alert.get_info()['type'], e), {'rule': rule['name']})
                alert_exception = str(e)
            else:
                self.alerts_sent += 1
                alert_sent = True

        # Write the alert(s) to ES
        agg_id = None
        for match in matches:
            alert_body = self.get_alert_body(match, rule, alert_sent, alert_time, alert_exception)
            # Set all matches to aggregate together
            if agg_id:
                alert_body['aggregate_id'] = agg_id
            res = self.writeback('elastalert', alert_body)
            if res and not agg_id:
                agg_id = res['_id']

    def get_alert_body(self, match, rule, alert_sent, alert_time, alert_exception=None):
        body = {'match_body': match}
        body['rule_name'] = rule['name']
        # TODO record info about multiple alerts
        body['alert_info'] = rule['alert'][0].get_info()
        body['alert_sent'] = alert_sent
        body['alert_time'] = alert_time

        # If the alert failed to send, record the exception
        if not alert_sent:
            body['alert_exception'] = alert_exception
        return body

    def writeback(self, doc_type, body):
        if self.debug:
            logging.info("Skipping writing to ES: %s" % (body))
            return None

        if '@timestamp' not in body:
            body['@timestamp'] = ts_now()
        if self.writeback_es:
            try:
                res = self.writeback_es.create(index=self.writeback_index,
                                               doc_type=doc_type, body=body)
                return res
            except ElasticsearchException as e:
                logging.exception("Error writing alert info to elasticsearch: %s" % (e))
                self.writeback_es = None
        return None

    def find_recent_pending_alerts(self, time_limit):
        """ Queries writeback_es to find alerts that did not send
        and are newer than time_limit """
        query = {'query': {'query_string': {'query': 'alert_sent:false'}},
                 'filter': {'range': {'alert_time': {'from': ts_add(ts_now(), -time_limit),
                                                     'to': ts_now()}}}}
        if self.writeback_es:
            try:
                res = self.writeback_es.search(index=self.writeback_index,
                                               doc_type='elastalert',
                                               body=query,
                                               size=1000)
                if res['hits']['hits']:
                    return res['hits']['hits']
            except:
                pass
        return []

    def send_pending_alerts(self):
        pending_alerts = self.find_recent_pending_alerts(self.alert_time_limit)
        for alert in pending_alerts:
            _id = alert['_id']
            alert = alert['_source']
            try:
                rule_name = alert.pop('rule_name')
                alert_time = alert.pop('alert_time')
                match_body = alert.pop('match_body')
            except KeyError:
                # Malformed alert, drop it
                continue

            agg_id = alert.get('aggregate_id', None)
            if agg_id:
                # Aggregated alerts will be taken care of by get_aggregated_matches
                continue

            # Find original rule
            for rule in self.rules:
                if rule['name'] == rule_name:
                    break
            else:
                # Original rule is missing, drop alert
                continue

            # Retry the alert unless it's a future alert
            if ts_delta(alert_time, ts_now()) > datetime.timedelta(0):
                aggregated_matches = self.get_aggregated_matches(_id)
                if aggregated_matches:
                    matches = [match_body] + [agg_match['match_body'] for agg_match in aggregated_matches]
                    self.alert(matches, rule, alert_time=alert_time)
                    rule['current_aggregate_id'] = None
                else:
                    self.alert([match_body], rule, alert_time=alert_time)

                # Delete it from the index
                try:
                    self.writeback_es.delete(index=self.writeback_index,
                                             doc_type='elastalert',
                                             id=_id)
                except:
                    self.handle_error("Failed to delete alert %s at %s" % (_id, alert_time))

        # Send in memory aggregated alerts
        for rule in self.rules:
            if rule['agg_matches']:
                if ts_delta(rule['aggregate_alert_time'], ts_now()) > datetime.timedelta(0):
                    self.alert(rule['agg_matches'], rule)
                    rule['agg_matches'] = []

    def get_aggregated_matches(self, _id):
        """ Removes and returns all matches from writeback_es that have aggregate_id == _id """
        query = {'query': {'query_string': {'query': 'aggregate_id:%s' % (_id)}}}
        matches = []
        if self.writeback_es:
            try:
                res = self.writeback_es.search(index=self.writeback_index,
                                               doc_type='elastalert',
                                               body=query)
                for match in res['hits']['hits']:
                    matches.append(match['_source'])
                    self.writeback_es.delete(index=self.writeback_index,
                                             doc_type='elastalert',
                                             id=match['_id'])
            except (KeyError, ElasticsearchException) as e:
                self.handle_error("Error fetching aggregated matches: %s" % (e), {'id': _id})
        return matches

    def add_aggregated_alert(self, match, rule):
        """ Save a match as a pending aggregate alert to elasticsearch. """
        if not rule['current_aggregate_id'] or rule['aggregate_alert_time'] < ts_to_dt(match[rule['timestamp_field']]):
            # First match, set alert_time
            match_time = ts_to_dt(match[rule['timestamp_field']])
            alert_time = match_time + rule['aggregation']
            rule['aggregate_alert_time'] = alert_time
            agg_id = None
        else:
            # Already pending aggregation, use existing alert_time
            alert_time = rule['aggregate_alert_time']
            agg_id = rule['current_aggregate_id']
            logging.info('Adding alert for %s to aggregation, next alert at %s' % (rule['name'], alert_time))

        alert_body = self.get_alert_body(match, rule, False, alert_time)
        if agg_id:
            alert_body['aggregate_id'] = agg_id
        res = self.writeback('elastalert', alert_body)

        # If new aggregation, save _id
        if res and not agg_id:
            rule['current_aggregate_id'] = res['_id']

        # Couldn't write the match to ES, save it in memory for now
        if not res:
            rule['agg_matches'].append(match)

        return res

    def silence(self):
        """ Silence an alert for a period of time. --silence and --rule must be passed as args. """
        if not self.args.rule:
            logging.error('--silence must be used with --rule')
            exit(1)

        # With --rule, self.rules will only contain that specific rule
        rule_name = self.rules[0]['name']

        try:
            unit, num = self.args.silence.split('=')
            silence_time = datetime.timedelta(**{unit: int(num)})
            silence_ts = dt_to_ts(silence_time + datetime.datetime.utcnow())
        except (ValueError, TypeError):
            logging.error('%s is not a valid time period' % (self.args.silence))
            exit(1)

        if not self.set_realert(rule_name, silence_ts):
            logging.error('Failed to save silence command to elasticsearch')
            exit(1)

        logging.info('Success. %s will be silenced until %s' % (rule_name, silence_ts))

    def set_realert(self, rule_name, timestamp):
        """ Write a silence to elasticsearch for rule_name until timestamp. """
        body = {'rule_name': rule_name,
                '@timestamp': ts_now(),
                'until': timestamp}
        self.silence_cache[rule_name] = timestamp
        return self.writeback('silence', body)

    def is_silenced(self, rule_name):
        """ Checks if rule_name is currently silenced. Returns false on exception. """
        if rule_name in self.silence_cache:
            if ts_delta(self.silence_cache[rule_name], ts_now()) < datetime.timedelta(0):
                return True
            else:
                self.silence_cache.pop(rule_name)
                return False

        query = {'filter': {'term': {'rule_name': rule_name}},
                 'sort': {'until': {'order': 'desc'}}}

        if self.writeback_es:
            try:
                res = self.writeback_es.search(index=self.writeback_index, doc_type='silence',
                                               size=1, body=query, _source_include=['until'])
            except ElasticsearchException as e:
                self.handle_error("Error while querying for alert silence status: %s" % (e), {'rule': rule_name})

                return False

            if res['hits']['hits']:
                until_ts = res['hits']['hits'][0]['_source']['until']
                if ts_delta(until_ts, ts_now()) < datetime.timedelta(0):
                    self.silence_cache[rule_name] = until_ts
                    return True
        return False

    def handle_error(self, message, data=None):
        ''' Logs message at error level and writes message, data and traceback to Elasticsearch. '''
        if not self.writeback_es:
            self.writeback_es = Elasticsearch(host=self.es_host, port=self.es_port)
        logging.error(message)
        body = {'message': message}
        tb = traceback.format_exc()
        body['traceback'] = tb.strip().split('\n')
        if data:
            body['data'] = data
        self.writeback('elastalert_error', body)


if __name__ == '__main__':
    client = ElastAlerter(sys.argv[1:])
    if not client.args.silence:
        client.start()
