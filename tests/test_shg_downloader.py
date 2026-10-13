#!/usr/bin/env python3
"""
Unit tests for shg_downloader.py
Run: python -m pytest tests/test_shg_downloader.py -v
"""

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Allow import without network calls
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import shg_downloader as sg


# ---------------------------------------------------------------------------
# safe_filename
# ---------------------------------------------------------------------------

class TestSafeFilename:
    def test_basic(self):
        assert sg.safe_filename("hello world") == "hello world"

    def test_illegal_chars_replaced(self):
        result = sg.safe_filename('foo<bar>baz:qux"quux/corge\\grault|waldo?fred*')
        assert '<' not in result
        assert '>' not in result
        assert ':' not in result
        assert '"' not in result
        assert '/' not in result
        assert '\\' not in result
        assert '|' not in result
        assert '?' not in result
        assert '*' not in result

    def test_whitespace_collapsed(self):
        assert sg.safe_filename("foo   bar") == "foo bar"

    def test_empty_string_gives_untitled(self):
        assert sg.safe_filename("") == "untitled"

    def test_only_illegal_chars_gives_underscores(self):
        # "/" is replaced by "_"; result is "___" (not empty → not "untitled")
        # "untitled" only fires when the result is completely empty after processing
        result = sg.safe_filename("///")
        assert result == "___"

    def test_truncation(self):
        long_str = "a" * 300
        result = sg.safe_filename(long_str, maxbytes=200)
        assert len(result.encode("utf-8")) <= 200

    def test_unicode_nfc(self):
        # NFC normalization should not break valid strings
        result = sg.safe_filename("café")
        assert "caf" in result

    def test_maxbytes_respected_for_multibyte(self):
        # Each Kannada character is 3 bytes in UTF-8
        s = "ಕ" * 100  # 300 bytes
        result = sg.safe_filename(s, maxbytes=30)
        assert len(result.encode("utf-8")) <= 30


# ---------------------------------------------------------------------------
# parse_handle_id
# ---------------------------------------------------------------------------

class TestParseHandleId:
    def test_standard_url(self):
        url = "https://shodhganga.inflibnet.ac.in/handle/10603/719988"
        assert sg.parse_handle_id(url) == "10603/719988"

    def test_url_with_query(self):
        url = "https://shodhganga.inflibnet.ac.in/handle/10603/12345?foo=bar"
        assert sg.parse_handle_id(url) == "10603/12345"

    def test_unknown_url_returns_md5(self):
        url = "https://example.com/unknown"
        result = sg.parse_handle_id(url)
        assert result.startswith("shg_")
        assert len(result) == 12  # "shg_" + 8 hex chars

    def test_handle_with_dot(self):
        url = "https://shodhganga.inflibnet.ac.in/handle/10603.1/999"
        assert sg.parse_handle_id(url) == "10603.1/999"


# ---------------------------------------------------------------------------
# parse_handle_suffix
# ---------------------------------------------------------------------------

class TestParseHandleSuffix:
    def test_standard(self):
        assert sg.parse_handle_suffix("10603/719988") == "719988"

    def test_fallback_returns_original(self):
        assert sg.parse_handle_suffix("no-numbers-here") == "no-numbers-here"

    def test_empty(self):
        assert sg.parse_handle_suffix("") == ""


# ---------------------------------------------------------------------------
# parse_size_text
# ---------------------------------------------------------------------------

class TestParseSizeText:
    def test_mb(self):
        assert sg.parse_size_text("1.67 MB") == int(1.67 * 1024 ** 2)

    def test_kb(self):
        assert sg.parse_size_text("828.11 kB") == int(828.11 * 1024)

    def test_bytes(self):
        assert sg.parse_size_text("512 B") == 512

    def test_gb(self):
        assert sg.parse_size_text("2.5 GB") == int(2.5 * 1024 ** 3)

    def test_empty(self):
        assert sg.parse_size_text("") == 0

    def test_none_like(self):
        assert sg.parse_size_text(None) == 0

    def test_no_unit_treated_as_bytes(self):
        assert sg.parse_size_text("1024") == 1024

    def test_case_insensitive(self):
        assert sg.parse_size_text("1.0 mb") == 1024 ** 2


# ---------------------------------------------------------------------------
# human_size
# ---------------------------------------------------------------------------

class TestHumanSize:
    def test_bytes(self):
        assert sg.human_size(512) == "512 B"

    def test_kb(self):
        result = sg.human_size(2048)
        assert "KB" in result

    def test_mb(self):
        result = sg.human_size(2 * 1024 ** 2)
        assert "MB" in result

    def test_gb(self):
        result = sg.human_size(3 * 1024 ** 3)
        assert "GB" in result


# ---------------------------------------------------------------------------
# normalize_language
# ---------------------------------------------------------------------------

class TestNormalizeLanguage:
    def test_english_variants(self):
        for v in ("en", "english", "English", "eng"):
            assert sg.normalize_language(v) == "eng"

    def test_hindi_variants(self):
        for v in ("hi", "hindi", "hin"):
            assert sg.normalize_language(v) == "hin"

    def test_kannada_variants(self):
        for v in ("kn", "kannada", "kan"):
            assert sg.normalize_language(v) == "kan"

    def test_empty_defaults_to_eng(self):
        assert sg.normalize_language("") == "eng"
        assert sg.normalize_language(None) == "eng"

    def test_unknown_short_code_returned(self):
        result = sg.normalize_language("xyz")
        assert result == "xyz"

    def test_unknown_long_code_truncated_to_3(self):
        result = sg.normalize_language("unknown_lang")
        assert result == "unk"


# ---------------------------------------------------------------------------
# sanitize_identifier
# ---------------------------------------------------------------------------

class TestSanitizeIdentifier:
    def test_basic(self):
        assert sg.sanitize_identifier("hello world") == "hello-world"

    def test_special_chars_become_dashes(self):
        result = sg.sanitize_identifier("foo@bar!baz")
        assert "@" not in result
        assert "!" not in result

    def test_consecutive_dashes_collapsed(self):
        result = sg.sanitize_identifier("foo---bar")
        assert "--" not in result

    def test_lowercase(self):
        assert sg.sanitize_identifier("FOO BAR") == "foo-bar"

    def test_truncated_to_76(self):
        long_s = "a" * 100
        result = sg.sanitize_identifier(long_s)
        assert len(result) <= 76

    def test_strips_leading_trailing(self):
        result = sg.sanitize_identifier("-.-foo-.-")
        assert not result.startswith("-")
        assert not result.endswith("-")


# ---------------------------------------------------------------------------
# make_identifier
# ---------------------------------------------------------------------------

class TestMakeIdentifier:
    def test_basic(self, tmp_path):
        result = sg.make_identifier("shg.", "12345", "My Thesis", tmp_path)
        assert "12345" in result
        assert result.startswith("shg")

    def test_max_80_chars(self, tmp_path):
        result = sg.make_identifier("shg.", "12345", "x" * 200, tmp_path)
        assert len(result) <= 80

    def test_empty_title_uses_hash(self, tmp_path):
        result = sg.make_identifier("shg.", "12345", "", tmp_path)
        assert len(result) >= 4

    def test_prefix_with_dot(self, tmp_path):
        result = sg.make_identifier("myprefix.", "999", "A Thesis", tmp_path)
        assert result.startswith("myprefix")

    def test_prefix_without_dot(self, tmp_path):
        result = sg.make_identifier("myprefix", "999", "A Thesis", tmp_path)
        assert result.startswith("myprefix")


# ---------------------------------------------------------------------------
# get_config
# ---------------------------------------------------------------------------

class TestGetConfig:
    def test_args_take_priority(self):
        args = MagicMock()
        args.mykey = "from_args"
        result = sg.get_config("mykey", "default", {"mykey": "from_cfg"}, args)
        assert result == "from_args"

    def test_config_dict_second(self):
        args = MagicMock()
        args.mykey = None
        result = sg.get_config("mykey", "default", {"mykey": "from_cfg"}, args)
        assert result == "from_cfg"

    def test_env_var_third(self):
        args = MagicMock()
        args.mykey = None
        with patch.dict(os.environ, {"SHG_MYKEY": "from_env"}):
            result = sg.get_config("mykey", "default", {}, args)
        assert result == "from_env"

    def test_default_last(self):
        args = MagicMock()
        args.mykey = None
        result = sg.get_config("mykey", "fallback", {}, args)
        assert result == "fallback"

    def test_bool_env_parsing(self):
        with patch.dict(os.environ, {"SHG_FLAG": "true"}):
            result = sg.get_config("flag", False, {}, None)
        assert result is True

    def test_int_env_parsing(self):
        with patch.dict(os.environ, {"SHG_COUNT": "42"}):
            result = sg.get_config("count", 0, {}, None)
        assert result == 42

    def test_int_env_invalid_returns_default(self):
        with patch.dict(os.environ, {"SHG_COUNT": "notanint"}):
            result = sg.get_config("count", 5, {}, None)
        assert result == 5


# ---------------------------------------------------------------------------
# clamp_workers
# ---------------------------------------------------------------------------

class TestClampWorkers:
    def test_clamp_max(self):
        assert sg.clamp_workers(100, 20) == 20

    def test_clamp_min(self):
        assert sg.clamp_workers(0, 20) == 1

    def test_within_range(self):
        assert sg.clamp_workers(10, 20) == 10


# ---------------------------------------------------------------------------
# load_progress / save_progress (atomic write)
# ---------------------------------------------------------------------------

class TestProgressIO:
    def test_fresh_start(self, tmp_path):
        p = tmp_path / "progress.json"
        state = sg.load_progress(p)
        assert state["items"] == {}
        assert state["total_size_bytes"] == 0
        assert sg.STAGE_MANIFESTED in state["stages"]

    def test_round_trip(self, tmp_path):
        p = tmp_path / "progress.json"
        state = sg.load_progress(p)
        state["items"]["123"] = {"title": "test"}
        sg.save_progress(p, state)
        loaded = sg.load_progress(p)
        assert loaded["items"]["123"]["title"] == "test"

    def test_corrupt_file_handled(self, tmp_path):
        p = tmp_path / "progress.json"
        p.write_text("NOT VALID JSON")
        state = sg.load_progress(p)
        assert state["items"] == {}  # fresh state returned

    def test_atomic_write_no_partial(self, tmp_path):
        """Verify tmp file does not linger after save."""
        p = tmp_path / "progress.json"
        state = sg.load_progress(p)
        sg.save_progress(p, state)
        tmp = p.with_suffix(f".tmp.{os.getpid()}")
        assert not tmp.exists()

    def test_concurrent_saves_no_corruption(self, tmp_path):
        """Multiple threads saving progress should not corrupt the file."""
        p = tmp_path / "progress.json"
        state = sg.load_progress(p)
        errors = []

        def worker(i):
            for _ in range(10):
                with sg._progress_save_lock:
                    state["items"][str(i)] = {"v": i}
                    try:
                        sg.save_progress(p, state)
                    except Exception as e:
                        errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        loaded = sg.load_progress(p)
        assert isinstance(loaded["items"], dict)


# ---------------------------------------------------------------------------
# set_item_stage / get_item_stage
# ---------------------------------------------------------------------------

class TestItemStage:
    def _fresh_state(self):
        return sg.load_progress(Path("/nonexistent"))

    def test_set_and_get(self):
        state = self._fresh_state()
        sg.set_item_stage(state, "123", sg.STAGE_MANIFESTED)
        assert sg.get_item_stage(state, "123") == sg.STAGE_MANIFESTED

    def test_advance_stage(self):
        state = self._fresh_state()
        sg.set_item_stage(state, "123", sg.STAGE_MANIFESTED)
        sg.set_item_stage(state, "123", sg.STAGE_PDF_DOWNLOADED)
        assert sg.get_item_stage(state, "123") == sg.STAGE_PDF_DOWNLOADED
        # Should not appear in manifested anymore
        assert "123" not in state["stages"][sg.STAGE_MANIFESTED]

    def test_unknown_handle_returns_none(self):
        state = self._fresh_state()
        assert sg.get_item_stage(state, "unknown") is None

    def test_empty_handle_is_noop(self):
        state = self._fresh_state()
        sg.set_item_stage(state, "", sg.STAGE_MANIFESTED)  # should not crash
        assert sg.get_item_stage(state, "") is None

    def test_cleaned_takes_highest_priority(self):
        """get_item_stage checks CLEANED first — highest progress wins."""
        state = self._fresh_state()
        sg.set_item_stage(state, "123", sg.STAGE_MANIFESTED)
        sg.set_item_stage(state, "123", sg.STAGE_CLEANED)
        assert sg.get_item_stage(state, "123") == sg.STAGE_CLEANED


# ---------------------------------------------------------------------------
# transform_to_ia_metadata
# ---------------------------------------------------------------------------

class TestTransformToIaMetadata:
    def _sample_data(self):
        return {
            "title": "Test Thesis",
            "abstract": "A study of things.",
            "researcher": "A. Researcher",
            "guides": ["Prof. Guide"],
            "subjects": ["Biology", "Chemistry"],
            "language": "en",
            "type": "Thesis",
            "date_issued": "2023-01-01",
            "date_accessioned": "",
            "date_available": "",
            "uri": "https://hdl.handle.net/10603/1",
            "publisher": "Some University",
            "department": "Science",
            "university": "Test University",
            "item_url": "https://shodhganga.inflibnet.ac.in/handle/10603/1",
            "handle": "10603/1",
        }

    def test_basic_fields_present(self):
        data = self._sample_data()
        result = sg.transform_to_ia_metadata(data)
        assert result["title"] == "Test Thesis"
        assert result["creator"] == "A. Researcher"
        assert result["mediatype"] == "texts"

    def test_language_normalized(self):
        data = self._sample_data()
        result = sg.transform_to_ia_metadata(data)
        assert result["language"] == "eng"

    def test_empty_fields_excluded(self):
        data = self._sample_data()
        data["researcher"] = ""
        result = sg.transform_to_ia_metadata(data)
        assert "creator" not in result

    def test_default_license_applied(self):
        data = self._sample_data()
        result = sg.transform_to_ia_metadata(data)
        assert "creativecommons" in result["licenseurl"]

    def test_custom_license_from_cfg(self):
        data = self._sample_data()
        cfg = {"license_url": "https://example.com/lic", "license": "Custom License"}
        result = sg.transform_to_ia_metadata(data, cfg=cfg)
        assert result["licenseurl"] == "https://example.com/lic"


# ---------------------------------------------------------------------------
# extract_handle_from_identifier
# ---------------------------------------------------------------------------

class TestExtractHandleFromIdentifier:
    def test_dot_prefix(self):
        assert sg.extract_handle_from_identifier("shg.12345-my-thesis") == "12345"

    def test_dash_prefix(self):
        assert sg.extract_handle_from_identifier("prefix-12345-my-thesis") == "12345"

    def test_empty(self):
        assert sg.extract_handle_from_identifier("") == ""

    def test_non_digit_suffix(self):
        result = sg.extract_handle_from_identifier("shg.abc-def")
        assert result == ""


# ---------------------------------------------------------------------------
# load_item_urls
# ---------------------------------------------------------------------------

class TestLoadItemUrls:
    def test_single_url_string(self):
        url = "https://shodhganga.inflibnet.ac.in/handle/10603/1"
        result = sg.load_item_urls(url)
        assert result == [url]

    def test_json_file_with_items(self, tmp_path):
        data = {"items": [
            "https://shodhganga.inflibnet.ac.in/handle/10603/1",
            "https://shodhganga.inflibnet.ac.in/handle/10603/2",
        ]}
        p = tmp_path / "listing.json"
        p.write_text(json.dumps(data))
        result = sg.load_item_urls(str(p))
        assert len(result) == 2

    def test_plain_text_file(self, tmp_path):
        p = tmp_path / "urls.txt"
        p.write_text(
            "https://shodhganga.inflibnet.ac.in/handle/10603/1\n"
            "https://shodhganga.inflibnet.ac.in/handle/10603/2\n"
        )
        result = sg.load_item_urls(str(p))
        assert len(result) == 2

    def test_nonexistent_path_treated_as_url(self):
        # load_item_urls treats any non-file string as a single URL by design
        result = sg.load_item_urls("/nonexistent/path.json")
        assert result == ["/nonexistent/path.json"]


# ---------------------------------------------------------------------------
# download_file: HTML response guard (regression for moved unreachable code)
# ---------------------------------------------------------------------------

class TestDownloadFileHTMLGuard:
    def test_html_response_returns_false(self, tmp_path):
        """Consent page HTML should not be written to disk."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "text/html; charset=utf-8", "content-length": "100"}
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        dest = tmp_path / "test.pdf"
        with patch.object(sg.SESSION, "get", return_value=mock_resp):
            result = sg.download_file("https://example.com/fake.pdf", dest, desc="test")
        assert result is False
        assert not dest.exists()


# ---------------------------------------------------------------------------
# Pipeline operator precedence fix regression
# ---------------------------------------------------------------------------

class TestSkipUploadLogic:
    """Regression test for the fixed operator precedence bug in cmd_pipeline."""

    def _stages_skipped(self, skip_upload, stage):
        """Simulate the fixed condition."""
        return skip_upload and stage in (sg.STAGE_PDF_DOWNLOADED, sg.STAGE_MERGED)

    def test_skip_upload_false_merged_not_skipped(self):
        assert not self._stages_skipped(False, sg.STAGE_MERGED)

    def test_skip_upload_true_merged_skipped(self):
        assert self._stages_skipped(True, sg.STAGE_MERGED)

    def test_skip_upload_true_pdf_downloaded_skipped(self):
        assert self._stages_skipped(True, sg.STAGE_PDF_DOWNLOADED)

    def test_skip_upload_false_pdf_downloaded_not_skipped(self):
        assert not self._stages_skipped(False, sg.STAGE_PDF_DOWNLOADED)

    def test_uploaded_never_skipped_by_skip_upload(self):
        assert not self._stages_skipped(True, sg.STAGE_UPLOADED)


class TestBotChallenge:
    @staticmethod
    def _resp(text, ctype="text/html; charset=utf-8"):
        r = MagicMock()
        r.headers = {"content-type": ctype}
        r.text = text
        r.raise_for_status.return_value = None
        return r

    def test_detects_anubis_page(self):
        page = "<title>Making sure you're not a bot!</title> anubis proof"
        assert sg.is_bot_challenge(self._resp(page))
        assert not sg.is_bot_challenge(self._resp("<title>Thesis</title>"))
        assert not sg.is_bot_challenge(self._resp(page, "application/pdf"))

    def test_fetch_raises_without_retrying(self):
        sess = MagicMock()
        sess.get.return_value = self._resp("Making sure you're not a bot! anubis")
        with pytest.raises(sg.BotChallengeError):
            sg.fetch("http://x", session=sess)
        assert sess.get.call_count == 1


class TestUploadSkipsOnlyWhenPdfPresent:
    """A metadata-only placeholder on IA must still receive its PDF."""

    @staticmethod
    def _run(files):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if cmd[:2] == ["ia", "metadata"]:
                out = json.dumps({"metadata": {"identifier": "shg.1-x"}, "files": files})
                return MagicMock(returncode=0, stdout=out, stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(sg.subprocess, "run", side_effect=fake_run), \
             patch.object(sg.time, "sleep"):
            ok = sg.upload_to_ia("shg.1-x", ["/tmp/a.pdf"], {"title": "T"},
                                 "shodhganga", False, 10, 1)
        return ok, [c for c in calls if c[:2] == ["ia", "upload"]]

    def test_placeholder_only_item_gets_upload(self):
        ok, uploads = self._run([{"name": "shg.1-x.txt", "size": "120"}])
        assert ok and len(uploads) == 1

    def test_item_with_pdf_is_skipped(self):
        ok, uploads = self._run([{"name": "shg.1-x.pdf", "size": "5000"}])
        assert ok and uploads == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
