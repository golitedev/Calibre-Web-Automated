from pathlib import Path

import pytest

from cps.rating import normalize_rating, rating_to_calibre


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 0), (0.5, 0.5), (1, 1), (1.5, 1.5), (4, 4), (4.5, 4.5), (5, 5)],
)
def test_rating_values_are_preserved(value, expected):
    assert normalize_rating(value) == expected


@pytest.mark.unit
def test_rating_import_rounds_to_nearest_half_star_without_changing_calibre_scale():
    assert normalize_rating(4.24) == 4
    assert normalize_rating(4.26) == 4.5
    assert rating_to_calibre(4.5) == 9


@pytest.mark.unit
def test_rating_rejects_values_outside_the_five_star_scale():
    assert normalize_rating(-0.5) is None
    assert normalize_rating(5.5) is None
    assert rating_to_calibre("not-a-rating") is None


@pytest.mark.unit
def test_half_star_component_is_first_party_and_does_not_patch_vendor_plugin():
    component = (REPO_ROOT / "cps/static/js/half_star_rating.js").read_text(encoding="utf-8")
    styles = (REPO_ROOT / "cps/static/css/cwa.css").read_text(encoding="utf-8")
    hotfix_dockerfile = (REPO_ROOT / "Dockerfile.inkly-hotfix").read_text(encoding="utf-8")
    vendor = (REPO_ROOT / "cps/static/js/libs/bootstrap-rating-input.min.js").read_text(encoding="utf-8")
    assert "addEventListener" in component
    assert "CwaHalfStarRating" in component
    assert "rgb(255, 105, 180)" in styles
    assert "stroke-linejoin='round'" in styles
    assert "clip-path: inset" in styles
    assert "cps/static/css/cwa.css" in hotfix_dockerfile
    assert vendor == (REPO_ROOT / "cps/static/js/libs/bootstrap-rating-input.min.js").read_text(encoding="utf-8")


@pytest.mark.unit
def test_rating_templates_keep_decimal_values_and_normal_form_field():
    detail = (REPO_ROOT / "cps/templates/detail.html").read_text(encoding="utf-8")
    edit = (REPO_ROOT / "cps/templates/book_edit.html").read_text(encoding="utf-8")
    assert "(entry.ratings[0].rating / 2)|int" not in detail
    assert "(book.ratings[0].rating / 2)|int" not in edit
    assert 'name="rating" id="rating-value"' in edit
    assert "data-cwa-half-star-rating" in detail
    assert "data-cwa-half-star-rating" in edit
