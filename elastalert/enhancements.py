# -*- coding: utf-8 -*-
from .util import pretty_ts
from datetime import datetime
from pytz import timezone


class BaseEnhancement(object):
    """ Enhancements take a match dictionary object and modify it in some way to
    enhance an alert. These are specified in each rule under the match_enhancements option.
    Generally, the key value pairs in the match module will be contained in the alert body. """

    def __init__(self, rule):
        self.rule = rule

    def process(self, match):
        """ Modify the contents of match, a dictionary, in some way """
        raise NotImplementedError()



class TimeEnhancement(BaseEnhancement):
    def process(self, match):
        # Convert UTC to desired timezone
        utc_time = match.get('createdDate')
        if utc_time:
            local_time = self.convert_utc_to_local(utc_time, 'Asia/Ho_Chi_Minh')
            match['createdDate'] = local_time

    def convert_utc_to_local(self, utc_dt_str, local_tz='Asia/Ho_Chi_Minh'):
        try:
            utc_dt = datetime.strptime(utc_dt_str, '%Y-%m-%dT%H:%M:%S.%f%z')
        except ValueError:
            try:
                utc_dt = datetime.strptime(utc_dt_str, '%Y-%m-%dT%H:%M:%S.%fZ')
            except ValueError:
                utc_dt = datetime.strptime(utc_dt_str, '%Y-%m-%dT%H:%M:%SZ')
        
        # Convert UTC datetime to local timezone
        local_tz = timezone(local_tz)
        local_dt = utc_dt.astimezone(local_tz)
        return local_dt.isoformat()


class DropMatchException(Exception):
    """ ElastAlert will drop a match if this exception type is raised by an enhancement """
    pass
