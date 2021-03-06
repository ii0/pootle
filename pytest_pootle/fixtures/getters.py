# -*- coding: utf-8 -*-
#
# Copyright (C) Pootle contributors.
#
# This file is a part of the Pootle project. It is distributed under the GPL3
# or later license. See the LICENSE file for a copy of the license and the
# AUTHORS file for copyright and authorship information.

from contextlib import contextmanager

import pytest

from pootle.core.contextmanagers import keep_data
from pootle.core.delegate import context_data, wordcount, tp_tool


@contextmanager
def _no_wordcount():
    with keep_data(signals=(wordcount, )):
        yield


@pytest.fixture
def no_wordcount():
    return _no_wordcount


@contextmanager
def _no_context_data():
    with keep_data(signals=(context_data, )):
        yield


@pytest.fixture
def no_context_data():
    return _no_context_data


@contextmanager
def _no_tp_tool():
    with keep_data(signals=(tp_tool, )):
        yield


@pytest.fixture
def no_tp_tool():
    return _no_tp_tool
