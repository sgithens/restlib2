import os

SECRET_KEY = '123abc'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': os.path.join(os.path.dirname(__file__), 'restlib2.db'),
    }
}

ROOT_URLCONF = 'tests.tests'

INSTALLED_APPS = (
    # TODO sgithens - There seems to an issue when these are included,
    # that the tests can't find the db tables to load the test fixtures.
    # However, the unit tests still pass if these are not included...
    # 'django.contrib.auth',
    # 'django.contrib.contenttypes',
    # 'restlib2',
    # 'tests',
)

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [
            "tests/templates/"
        ],
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.template.context_processors.debug',
                'django.template.context_processors.i18n',
                'django.template.context_processors.media',
                'django.template.context_processors.static',
                'django.template.context_processors.tz',
                'django.contrib.messages.context_processors.messages',
            ]
        }
    },
]

LOGGING = {
    'version': 1,
    'disable_existing_loggers': True,
    'handlers': {
        'console': {
            'level': 'DEBUG',
            'class': 'logging.StreamHandler',
        },
    },
    'loggers': {
        'restlib2': {
            'handlers': ['console'],
            'propagate': True,
            'level': 'DEBUG',
        },
    }
}
