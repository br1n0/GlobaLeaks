# -*- coding: UTF-8
#
#   logger
#   ******
#
# Log.admin( alarm_level=[$,$], code=$INT, args={'name':'stuff', 'other':'stuff'} )
#
# adminLog, receiverLog, tipLog are @classmethod, callable by any stuff in the world

import cgi
import codecs
import logging
import os
import sys
import traceback

from twisted.python import log as twlog
from twisted.python import logfile as twlogfile
from twisted.python import util
from twisted.python.failure import Failure

from globaleaks.utils.tempobj import TempObj
from globaleaks.utils.utility import ISO8601_to_datetime, datetime_to_ISO8601, datetime_now

from globaleaks.settings import GLSettings


_LOG_CODE = {
    # format: UNIQUE_CODE : [ "String with optionally %d %s..", NUMBER_OF_ARGS ]
    1 : [ "Something is happen without argument ", 0 ],
    2 : [ "Something is happen with an argument, look: %s", 1 ],
    3 : [ "Something is happen with many details %s %s %d", 3],
    4 : [ "Something boring here", 0 ],

    # Something more serious for the first test
    5 : [ "Admin logged in the system", 0 ],
    6 : [ "Receiver %s logged in the system", 1 ],

    # Used for Receiver
    7 : [ "You logged in the system ", 0 ]
}

def _log_parameter_check(alarm_level, code, args):
    """
    This function is intended to verify that the GlobaLeaks developer
    is not making mistakes. Checks the integrity of the log data

    :param alarm_level: list or keyword between 'normal', 'warning', 'mail'
    :param code: a numeric unique identifier of the error, usable for translations
    :param args: list of argument.

    :return: No return, or AssertionError
    """
    acceptable_level = ['normal', 'warning', 'mail']

    if isinstance(alarm_level, list):
        for al in alarm_level:
            assert al in acceptable_level, \
                "%s not in %s" % (al, acceptable_level)
    else:
        assert alarm_level in acceptable_level, \
            "%s not in %s" % (alarm_level, acceptable_level)

    assert isinstance(code, int), "Log Code %s has to be an Integer" % code
    assert code in _LOG_CODE, "Log Code %d is not implemented yet" % code

    assert isinstance(args, list), "Expected a list as argument, not %s" % type(args)
    assert len(args) == _LOG_CODE[code][1], "Invalid number of arguments, expected %d got %d" % (
        _LOG_CODE[code][1], len(args)
    )


def adminLog(alarm_level, code, args):
    _log_parameter_check(alarm_level, code, args)
    LoggedEvent({
        'code' : code,
        'args' : args
    }, subject='admin')

def receiverLog(alarm_level, code, args, user_id):
    _log_parameter_check(alarm_level, code, args)
    LoggedEvent({
        'code' : code,
        'args' : args
    }, subject='receiver', subject_id=user_id)

def tipLog(alarm_level, code, args, tip_id):
    _log_parameter_check(alarm_level, code, args)
    LoggedEvent({
        'code' : code,
        'args' : args
    }, subject='itip', subject_id=tip_id)



reactor_override = None

class LoggedEvent(TempObj):
    """
    This
    """

    _incremental_id = 0
    LogQueue = dict()

    @classmethod
    def get(cls, log_id):
        return LoggedEvent.LogQueue[log_id]

    @classmethod
    def get_unique_log_id(cls):
        cls._incremental_id += 1
        return cls._incremental_id

    @classmethod
    def get_last_log_id(cls):
        return cls._incremental_id

    def serialize_log(self):
        return {
            'log_code': self.log_code,
            'msg': _LOG_CODE[self.log_code][0],
            'args': self.args,
            'log_date': datetime_to_ISO8601(self.log_date),
            # 'repeated': self.repeated,
            'subject': self.subject_kind,
            'subject_id': self.subject_id,
        }

    def __init__(self, log_info, subject, subject_id=None, debug=False):

        self.log_code = log_info['code']
        self.args = log_info['args']
        self.log_date = datetime_now()
        self.subject_kind = subject
        self.subject_id = subject_id
        self.debug = debug
        self.id = LoggedEvent.get_unique_log_id()

        TempObj.__init__(self,
                         LoggedEvent.LogQueue,
                         self.id,
                         # seconds of validity, depends on the flushing ratio,
                         60 * 10,
                         reactor_override)

        self.expireCallbacks.append(self.id)


########## copied from utility.py to put all the log related function here
########## They has to be updated anyway


def log_encode_html(s):
    """
    This function encodes the following characters
    using HTML encoding: < > & ' " \ /

    This function has been suggested for security reason by an old PT, and
    make senses only if the Log can be influenced by external means. now with the
    new logging structure, only the "arguments" has to be escaped, not all the line in
    the logfile.
    """
    s = cgi.escape(s, True)
    s = s.replace("'", "&#39;")
    s = s.replace("/", "&#47;")
    s = s.replace("\\", "&#92;")
    return s

def log_remove_escapes(s):
    """
    This function removes escape sequence from log strings, read the comment in the function above
    """
    if isinstance(s, unicode):
        return codecs.encode(s, 'unicode_escape')
    else:
        try:
            s = str(s)
            unicodelogmsg = s.decode('utf-8')
        except UnicodeDecodeError:
            return codecs.encode(s, 'string_escape')
        except Exception as e:
            return "Failure in log_remove_escapes %r" % e
        else:
            return codecs.encode(unicodelogmsg, 'unicode_escape')

class GLLogObserver(twlog.FileLogObserver):
    suppressed = 0
    limit_suppressed = 1
    last_exception_msg = ""

    def emit(self, eventDict):
        if 'failure' in eventDict:
            vf = eventDict['failure']
            e_t, e_v, e_tb = vf.type, vf.value, vf.getTracebackObject()
            sys.excepthook(e_t, e_v, e_tb)

        text = twlog.textFromEventDict(eventDict)
        if text is None:
            return

        timeStr = self.formatTime(eventDict['time'])
        fmtDict = {'system': eventDict['system'], 'text': text.replace("\n", "\n\t")}
        msgStr = twlog._safeFormat("[%(system)s] %(text)s\n", fmtDict)

        if GLLogObserver.suppressed == GLLogObserver.limit_suppressed:
            # This code path flush the status of the broken log, in the case a flood is happen
            # for few moment or in the case something goes wrong when logging below.

            ##### log.info("!! has been suppressed %d log lines due to error flood (last error %s)" %
            #####          (GLLogObserver.limit_suppressed, GLLogObserver.last_exception_msg) )

            GLLogObserver.suppressed = 0
            GLLogObserver.limit_suppressed += 5
            GLLogObserver.last_exception_msg = ""

        try:
            # in addition to escape sequence removal on logfiles we also quote html chars
            util.untilConcludes(self.write, timeStr + " " + log_encode_html(msgStr))
            util.untilConcludes(self.flush) # Hoorj!
        except Exception as excep:
            GLLogObserver.suppressed += 1
            GLLogObserver.last_exception_msg = str(excep)


class Logger(object):
    """
    Customized LogPublisher
    """
    def _str(self, msg):
        if isinstance(msg, unicode):
            msg = msg.encode('utf-8')

        return log_remove_escapes(msg)

    def exception(self, error):
        """
        Error can either be an error message to print to stdout and to the logfile
        or it can be a twisted.python.failure.Failure instance.
        """
        if isinstance(error, Failure):
            error.printTraceback()
        else:
            exc_type, exc_value, exc_traceback = sys.exc_info()
            traceback.print_exception(exc_type, exc_value, exc_traceback)

    def info(self, msg):
        if GLSettings.loglevel and GLSettings.loglevel <= logging.INFO:
            print "[-] %s" % self._str(msg)

    def err(self, msg):
        if GLSettings.loglevel:
            twlog.err("[!] %s" % self._str(msg))

    def debug(self, msg):
        if GLSettings.loglevel and GLSettings.loglevel <= logging.DEBUG:
            print "[D] %s" % self._str(msg)

    def time_debug(self, msg):
        # read the command in settings.py near 'verbosity_dict'
        if GLSettings.loglevel and GLSettings.loglevel <= (logging.DEBUG - 1):
            print "[T] %s" % self._str(msg)

    def msg(self, msg):
        if GLSettings.loglevel:
            twlog.msg("[ ] %s" % self._str(msg))

    def start_logging(self):
        """
        If configured enables logserver
        """
        twlog.startLogging(sys.stdout)
        if GLSettings.logfile:
            name = os.path.basename(GLSettings.logfile)
            directory = os.path.dirname(GLSettings.logfile)

            logfile = twlogfile.LogFile(name, directory,
                                        rotateLength=GLSettings.log_file_size,
                                        maxRotatedFiles=GLSettings.maximum_rotated_log_files)
            twlog.addObserver(GLLogObserver(logfile).emit)
