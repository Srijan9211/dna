"""The small expiring cache the tracker providers share."""

from dna.prodtrack_providers.ttl_cache import TTLCache


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_a_value_is_served_until_it_expires():
    clock = Clock()
    cache = TTLCache(10, clock=clock)
    cache.put("k", [1])
    assert cache.get("k") == (True, [1])
    clock.now += 9.9
    assert cache.get("k") == (True, [1])
    clock.now += 0.2
    assert cache.get("k") == (False, None)


def test_a_missing_key_is_a_miss_even_when_the_stored_value_is_falsy():
    cache = TTLCache(10)
    cache.put("empty", [])
    assert cache.get("empty") == (True, [])
    assert cache.get("other") == (False, None)


def test_the_cache_is_bounded_and_forgets_the_least_recently_used():
    cache = TTLCache(10, max_entries=3)
    for key in "abc":
        cache.put(key, key)
    cache.get("a")  # a is now the freshest
    cache.put("d", "d")
    assert len(cache) == 3
    assert cache.get("b") == (False, None)
    assert cache.get("a")[0] and cache.get("d")[0]


def test_clear_empties_it():
    cache = TTLCache(10)
    cache.put("k", 1)
    cache.clear()
    assert len(cache) == 0
