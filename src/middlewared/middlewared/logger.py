import logging
from logging.config import dictConfig
import logging.handlers
import os

import sentry_sdk

from .utils import sw_version, sw_version_is_stable, MIDDLEWARE_RUN_DIR


# markdown debug is also considered useless
logging.getLogger('MARKDOWN').setLevel(logging.INFO)
# asyncio runs in debug mode but we do not need INFO/DEBUG
logging.getLogger('asyncio').setLevel(logging.WARN)
# We dont need internal aiohttp debug logging
logging.getLogger('aiohttp.internal').setLevel(logging.WARN)
# We dont need internal botocore debug logging
logging.getLogger('botocore').setLevel(logging.WARN)
# we dont need websocket debug messages
logging.getLogger('websocket').setLevel(logging.CRITICAL)
# we dont need GitPython debug messages (used in catalogs)
logging.getLogger('git.cmd').setLevel(logging.CRITICAL)
logging.getLogger('git.repo').setLevel(logging.CRITICAL)
# issues garbage warnings
logging.getLogger('googleapiclient').setLevel(logging.ERROR)
# registered 'pbkdf2_sha256' handler: <class 'passlib.handlers.pbkdf2.pbkdf2_sha256'>
logging.getLogger('passlib.registry').setLevel(logging.INFO)
# pyroute2.ndb is chatty....only log errors
logging.getLogger('pyroute2.ndb').setLevel(logging.CRITICAL)
logging.getLogger('pyroute2.netlink').setLevel(logging.CRITICAL)
logging.getLogger('pyroute2.netlink.nlsocket').setLevel(logging.CRITICAL)
# It logs each call made to the k8s api server when in debug mode, so we set the level to warn
logging.getLogger('kubernetes_asyncio.client.rest').setLevel(logging.WARN)
logging.getLogger('kubernetes_asyncio.config.kube_config').setLevel(logging.WARN)
logging.getLogger('urllib3').setLevel(logging.WARNING)
# ACME is very verbose in logging the request it sends with headers etc, let's not pollute the logs
# with that much information and raise the log level in this case
logging.getLogger('acme.client').setLevel(logging.WARN)
logging.getLogger('certbot_dns_cloudflare._internal.dns_cloudflare').setLevel(logging.WARN)


LOGFILE = '/var/log/middlewared.log'
ZETTAREPL_LOGFILE = '/var/log/zettarepl.log'
FAILOVER_LOGFILE = '/var/log/failover.log'
logging.TRACE = 6


def trace(self, message, *args, **kws):
    if self.isEnabledFor(logging.TRACE):
        self._log(logging.TRACE, message, args, **kws)


logging.addLevelName(logging.TRACE, "TRACE")
logging.Logger.trace = trace


class CrashReporting(object):
    enabled_in_settings = False

    """
    Pseudo-Class for remote crash reporting
    """

    def __init__(self):
        if sw_version_is_stable():
            self.sentinel_file_path = f'{MIDDLEWARE_RUN_DIR}/.crashreporting_disabled'
        else:
            self.sentinel_file_path = '/data/.crashreporting_disabled'
        self.logger = logging.getLogger('middlewared.logger.CrashReporting')
        url = 'https://11101daa5d5643fba21020af71900475:d60cd246ba684afbadd479653de2c216@sentry.ixsystems.com/2'
        query = '?timeout=3'
        sentry_sdk.init(
            url + query,
            release=sw_version(),
            integrations=[],
            default_integrations=False,
        )
        sentry_sdk.utils.MAX_STRING_LENGTH = 10240
        # FIXME: remove this when 0.10.3 is released
        strip_string = sentry_sdk.utils.strip_string
        sentry_sdk.utils.strip_string = lambda s: strip_string(s, sentry_sdk.utils.MAX_STRING_LENGTH)
        sentry_sdk.utils.slim_string = sentry_sdk.utils.strip_string
        sentry_sdk.serializer.strip_string = sentry_sdk.utils.strip_string
        sentry_sdk.serializer.slim_string = sentry_sdk.utils.strip_string

    def is_disabled(self):
        """
        Check the existence of sentinel file and its absolute path
        against STABLE and DEVELOPMENT branches.

        Returns:
            bool: True if crash reporting is disabled, False otherwise.
        """
        # Allow report to be disabled via sentinel file or environment var,
        # if TrueNAS current train is STABLE, the sentinel file path will be /var/run/middleware/,
        # otherwise it's path will be /data/ and can be persistent.

        if not self.enabled_in_settings:
            return True

        if os.path.exists(self.sentinel_file_path) or 'CRASHREPORTING_DISABLED' in os.environ:
            return True

        if os.stat(__file__).st_dev != os.stat('/').st_dev:
            return True

        return False

    def report(self, exc_info, log_files):
        """"
        Args:
            exc_info (tuple): Same as sys.exc_info().
            request (obj, optional): It is the HTTP Request.
            sw_version (str): The current middlewared version.
            t_log_files (tuple): A tuple with log file absolute path and name.
        """
        if self.is_disabled():
            return

        data = {}
        for path, name in log_files:
            if os.path.exists(path):
                with open(path, 'r') as absolute_file_path:
                    contents = absolute_file_path.read()[-10240:]
                    data[name] = contents

        self.logger.debug('Sending a crash report...')
        try:
            with sentry_sdk.configure_scope() as scope:
                payload_size = 0
                for k, v in data.items():
                    if payload_size + len(v) < 190000:
                        scope.set_extra(k, v)
                        payload_size += len(v)
                sentry_sdk.capture_exception(exc_info)
        except Exception:
            self.logger.debug('Failed to send crash report', exc_info=True)


class LoggerFormatter(logging.Formatter):
    """Format the console log messages"""

    CONSOLE_COLOR_FORMATTER = {
        'YELLOW': '\033[1;33m',  # (warning)
        'GREEN': '\033[1;32m',  # (info)
        'RED': '\033[1;31m',  # (error)
        'HIGHRED': '\033[1;41m',  # (critical)
        'RESET': '\033[1;m',  # Reset
    }
    LOGGING_LEVEL = {
        'CRITICAL': 50,
        'ERROR': 40,
        'WARNING': 30,
        'INFO': 20,
        'DEBUG': 10,
        'NOTSET': 0
    }

    def format(self, record):
        """Set the color based on the log level.

            Returns:
                logging.Formatter class.
        """

        if record.levelno == self.LOGGING_LEVEL['CRITICAL']:
            color_start = self.CONSOLE_COLOR_FORMATTER['HIGHRED']
        elif record.levelno == self.LOGGING_LEVEL['ERROR']:
            color_start = self.CONSOLE_COLOR_FORMATTER['HIGHRED']
        elif record.levelno == self.LOGGING_LEVEL['WARNING']:
            color_start = self.CONSOLE_COLOR_FORMATTER['RED']
        elif record.levelno == self.LOGGING_LEVEL['INFO']:
            color_start = self.CONSOLE_COLOR_FORMATTER['GREEN']
        elif record.levelno == self.LOGGING_LEVEL['DEBUG']:
            color_start = self.CONSOLE_COLOR_FORMATTER['YELLOW']
        else:
            color_start = self.CONSOLE_COLOR_FORMATTER['RESET']

        color_reset = self.CONSOLE_COLOR_FORMATTER['RESET']

        record.levelname = color_start + record.levelname + color_reset

        return logging.Formatter.format(self, record)


class Logger(object):
    """Pseudo-Class for Logger - Wrapper for logging module"""
    def __init__(
        self, application_name, debug_level=None,
        log_format='[%(asctime)s] (%(levelname)s) %(name)s.%(funcName)s():%(lineno)d - %(message)s'
    ):
        self.application_name = application_name
        self.debug_level = debug_level or 'DEBUG'
        self.log_format = log_format

        self.DEFAULT_LOGGING = {
            'version': 1,
            'disable_existing_loggers': False,
            'loggers': {
                '': {
                    'level': 'NOTSET',
                    'handlers': ['file'],
                },
                'zettarepl': {
                    'level': 'NOTSET',
                    'handlers': ['zettarepl_file'],
                    'propagate': False,
                },
                'failover': {
                    'level': 'NOTSET',
                    'handlers': ['failover_file'],
                    'propagate': False,
                },
            },
            'handlers': {
                'file': {
                    'level': 'DEBUG',
                    'class': 'logging.handlers.RotatingFileHandler',
                    'filename': LOGFILE,
                    'mode': 'a',
                    'maxBytes': 10485760,
                    'backupCount': 5,
                    'encoding': 'utf-8',
                    'formatter': 'file',
                },
                'zettarepl_file': {
                    'level': 'DEBUG',
                    'class': 'logging.handlers.RotatingFileHandler',
                    'filename': ZETTAREPL_LOGFILE,
                    'mode': 'a',
                    'maxBytes': 10485760,
                    'backupCount': 5,
                    'encoding': 'utf-8',
                    'formatter': 'zettarepl_file',
                },
                'failover_file': {
                    'level': 'DEBUG',
                    'class': 'logging.handlers.RotatingFileHandler',
                    'filename': FAILOVER_LOGFILE,
                    'mode': 'a',
                    'maxBytes': 10485760,
                    'backupCount': 5,
                    'encoding': 'utf-8',
                    'formatter': 'file',
                },
            },
            'formatters': {
                'file': {
                    'format': self.log_format,
                    'datefmt': '%Y/%m/%d %H:%M:%S',
                },
                'zettarepl_file': {
                    'format': '[%(asctime)s] %(levelname)-8s [%(threadName)s] [%(name)s] %(message)s',
                    'datefmt': '%Y/%m/%d %H:%M:%S',
                },
            },
        }

    def getLogger(self):
        return logging.getLogger(self.application_name)

    def configure_logging(self, output_option='file'):
        """
        Configure the log output to file or console.
            `output_option` str: Default is `file`, can be set to `console`.
        """
        if output_option.lower() == 'console':
            console_handler = logging.StreamHandler()
            logging.root.setLevel(getattr(logging, self.debug_level))
            time_format = "%Y/%m/%d %H:%M:%S"
            console_handler.setFormatter(LoggerFormatter(self.log_format, datefmt=time_format))
            logging.root.addHandler(console_handler)
        else:
            dictConfig(self.DEFAULT_LOGGING)

            # Make sure various log files are not readable by everybody.
            # umask could be another approach but chmod was chosen so
            # it affects existing installs.
            for i in (LOGFILE, ZETTAREPL_LOGFILE, FAILOVER_LOGFILE):
                try:
                    os.chmod(i, 0o640)
                except OSError:
                    pass

        logging.root.setLevel(getattr(logging, self.debug_level))


def setup_logging(name, debug_level, log_handler):
    _logger = Logger(name, debug_level)
    _logger.getLogger()

    if log_handler == 'console':
        _logger.configure_logging('console')
    else:
        _logger.configure_logging('file')
