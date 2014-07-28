#!/usr/bin/env python
# encoding:utf-8

"""
    loggin utilities
    ~~~~~~~~~~~~~~~~

    :created: 2014 by Jens Diemer - www.jensdiemer.de
    :copyleft: 2014 by the DragonPy team, see AUTHORS for more details.
    :license: GNU GPL v3 or above, see LICENSE for more details.
"""

import logging
import sys
import multiprocessing


log = multiprocessing.log_to_stderr()

log.critical("Log handlers: %s", repr(log.handlers))
if len(log.handlers) > 1:# FIXME: tro avoid doublicated output
    log.handlers = (log.handlers[0],)
    log.critical("Fixed Log handlers: %s", repr(log.handlers))

def setup_logging(log, level, handler=None, log_formatter=None):
    """
    levels:
         1 - hardcode DEBUG ;)
        10 - DEBUG
        20 - INFO
        30 - WARNING
        40 - ERROR
        50 - CRITICAL/FATAL
    """
    sys.stderr.write("Set logging to %i\n" % level)
    log.setLevel(level)

    if handler is None:
        handler = logging.StreamHandler()

    if log_formatter is not None:
        handler.setFormatter(log_formatter)

    if hasattr(handler, "baseFilename"):
        sys.stderr.write("Log to file: %s (%s)\n" % (
            handler.baseFilename, repr(handler))
        )
    else:
        sys.stderr.write("Log to handler: %s\n" % repr(handler))
    log.handlers = (handler,)
