import logging.config

PROJECT_NAME = 'aqua valet'

DEFAULT_FORMATTER = {
        '()': 'waterbutler.core.logging.MaskFormatter',
        'format': '[%(asctime)s][%(levelname)s][%(name)s]: %(message)s',
        'pattern': '(?<=cookie=)(.*?)(?=&|$)',
        'mask': '***'
}
DEFAULT_LOGGING_CONFIG = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'console': DEFAULT_FORMATTER,
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'level': 'INFO',
            'formatter': 'console'
        },
        'syslog': {
            'class': 'logging.handlers.SysLogHandler',
            'level': 'INFO'
        }
    },
    'loggers': {
        '': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False
        }
    },
    'root': {
        'level': 'INFO',
        'handlers': ['console']
    }
}

DEBUG = True
OP_CONCURRENCY = 5

logging_config = DEFAULT_LOGGING_CONFIG
logging.config.dictConfig(logging_config)

SENTRY_DSN = None
