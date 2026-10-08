"""Brute-force protection on the password login."""

import pytest
from fastapi.testclient import TestClient

from dna import configsys
from dna.auth.throttle import LoginThrottle, reset_login_throttle


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def throttle(max_failures=3, window=60.0):
    clock = Clock()
    return LoginThrottle(max_failures, window, clock=clock), clock


def test_attempts_are_allowed_until_the_limit_is_reached():
    t, _ = throttle()
    for _ in range(2):
        t.failed("1.1.1.1", "bob")
    assert t.retry_after("1.1.1.1", "bob") == 0
    t.failed("1.1.1.1", "bob")
    assert t.retry_after("1.1.1.1", "bob") > 0


def test_the_wait_ends_when_the_oldest_failure_ages_out():
    t, clock = throttle()
    for _ in range(3):
        t.failed("1.1.1.1", "bob")
    clock.now += 30
    assert 0 < t.retry_after("1.1.1.1", "bob") <= 31
    clock.now += 31
    assert t.retry_after("1.1.1.1", "bob") == 0


def test_one_address_does_not_lock_another_out_of_the_same_account():
    t, _ = throttle()
    for _ in range(3):
        t.failed("1.1.1.1", "bob")
    assert t.retry_after("2.2.2.2", "bob") == 0
    assert t.retry_after("1.1.1.1", "alice") == 0


def test_many_addresses_guessing_one_account_are_stopped_eventually():
    t, _ = throttle()
    for i in range(9):  # three times the per-address limit
        t.failed(f"10.0.0.{i}", "Bob")
    assert t.retry_after("10.9.9.9", "bob") > 0  # a new address, same account


def test_the_user_name_is_not_case_sensitive():
    t, _ = throttle()
    for _ in range(3):
        t.failed("1.1.1.1", "BOB")
    assert t.retry_after("1.1.1.1", "bob") > 0


def test_a_successful_login_clears_that_pairs_failures():
    t, _ = throttle()
    for _ in range(2):
        t.failed("1.1.1.1", "bob")
    t.succeeded("1.1.1.1", "bob")
    t.failed("1.1.1.1", "bob")
    assert t.retry_after("1.1.1.1", "bob") == 0


def test_memory_is_bounded():
    t, _ = throttle()
    for i in range(10_050):
        t.failed(f"10.{i // 250}.{i % 250}.1", f"user{i}")
    assert len(t._failures) <= 10_000
