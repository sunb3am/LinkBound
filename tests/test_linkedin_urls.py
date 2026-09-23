import pytest

from app.linkedin_urls import canonical_profile_url


def test_profile_url_is_canonicalized_without_tracking_query():
    assert canonical_profile_url("linkedin.com/in/Ada-Lovelace/?trk=outreach") == "https://www.linkedin.com/in/Ada-Lovelace"


@pytest.mark.parametrize("url", [
    "https://evil.example/in/person",
    "https://linkedin.com.evil.example/in/person",
    "https://linkedin.com/feed/",
    "http://linkedin.com/in/person",
    "https://linkedin.com:443/in/person",
    "https://linkedin.com:broken/in/person",
])
def test_queue_rejects_urls_that_could_leave_profile_pages(url):
    with pytest.raises(ValueError):
        canonical_profile_url(url)
