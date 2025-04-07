
from setuptools import setup

setup(
    name='jack_delay-GUI',
    version='0.1.0',
    py_modules=['latency_test'],
    entry_points={
        'console_scripts': [
            'jack_delay-GUI = latency_test:main',
        ],
    },
)
