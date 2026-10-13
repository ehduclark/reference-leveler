"""Ensure retired batch processing cannot bypass protected rendering."""

from argparse import Namespace

import pytest

import reference_leveler as legacy


def test_legacy_batch_execution_is_disabled():
    with pytest.raises(RuntimeError, match="retired"):
        legacy.batch_cmd(Namespace())


def test_legacy_profile_command_remains_available():
    assert callable(legacy.profile_cmd)
