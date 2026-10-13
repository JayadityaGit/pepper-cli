"""``pepper groups`` room-acting commands: each reaches the right ``ApiClient`` method, and the
destructive ones ask first."""

from __future__ import annotations

import pytest

from pepper_cli import api_client, cli, credentials
from pepper_cli.secret import SecretStr
from tests.test_cli_groups import _message

GID = "00000000-0000-0000-0000-0000000000a1"
TID = "00000000-0000-0000-0000-0000000000b1"
MID = "00000000-0000-0000-0000-0000000000c1"
PID = "00000000-0000-0000-0000-0000000000d1"


class _Api:
    """Records every call; answers from ``canned`` by method name, else ``{}``."""

    calls: list[tuple] = []
    canned: dict = {}

    def __init__(self, **_: object) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def list_groups(self, *, archived=False):
        topic = api_client.GroupTopicSummary(TID, "General", "2026-10-02")
        room = api_client.GroupSummary(GID, "Trip", "#fff", "", 2, False, [topic])
        return api_client.GroupList(True, False, 1, 0, [] if archived else [room])

    def group_roster(self, group_id):
        return [api_client.GroupRosterMember(PID, "Dana")]

    def __getattr__(self, name):
        def call(*args, **kwargs):
            _Api.calls.append((name, *args, kwargs))
            result = _Api.canned.get(name, {})
            if isinstance(result, Exception):
                raise result
            return result

        return call


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    credentials.save(server_url="http://example.com", token=SecretStr("pep_pat_abc"))
    _Api.calls, _Api.canned = [], {}
    monkeypatch.setattr(api_client, "ApiClient", _Api)
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    return _Api


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (
            ["edit", "Trip", MID, "new", "text"],
            ("edit_group_message", GID, MID, {"body": "new text"}),
        ),
        (["react", "Trip", MID, "🎉"], ("react_to_group_message", GID, MID, {"emoji": "🎉"})),
        (["unreact", "Trip", MID, "🎉"], ("clear_group_reaction", GID, MID, {"emoji": "🎉"})),
        (["new-topic", "Trip", "Plans"], ("create_group_topic", GID, {"name": "Plans"})),
        (["rename-topic", "Trip", TID, "Docs"], ("rename_group_topic", GID, TID, {"name": "Docs"})),
        (
            ["archive-topic", "Trip", TID],
            ("set_group_topic_archived", GID, TID, {"archived": True}),
        ),
        (["rename", "Trip", "Trip 2"], ("rename_group", GID, {"name": "Trip 2"})),
        (["unarchive", "Trip"], ("set_group_archived", GID, {"archived": False})),
        (["mute", "Trip"], ("set_group_muted", GID, {"muted": True})),
        (["unmute", "Trip"], ("set_group_muted", GID, {"muted": False})),
        (["invite", "Trip", "a@b.co"], ("invite_to_group", GID, {"handle": "a@b.co"})),
        (["install-app", "Trip", "notes"], ("install_group_app", GID, {"app_key": "notes"})),
    ],
)
def test_command_reaches_its_api_method(api, argv, expected) -> None:
    assert cli.main(["groups", *argv]) == 0
    assert api.calls == [expected]


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["delete-message", "Trip", MID], ("delete_group_message", GID, MID, {})),
        (["remove", "Trip", "dana"], ("remove_group_member", GID, PID, {})),
        (["role", "Trip", PID, "owner"], ("set_group_member_role", GID, PID, {"role": "owner"})),
        (["archive", "Trip"], ("set_group_archived", GID, {"archived": True})),
        (["leave", "Trip"], ("leave_group", GID, {})),
    ],
)
def test_destructive_commands_need_yes_off_a_terminal(api, argv, expected) -> None:
    assert cli.main(["groups", *argv]) == 2
    assert api.calls == []
    assert cli.main(["groups", *argv, "--yes"]) == 0
    assert api.calls == [expected]


def test_destructive_command_asks_on_a_terminal(api, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_prompt", lambda _: "n")
    assert cli.main(["groups", "leave", "Trip"]) == 0
    assert api.calls == []
    monkeypatch.setattr(cli, "_prompt", lambda _: "y")
    assert cli.main(["groups", "leave", "Trip"]) == 0
    assert api.calls == [("leave_group", GID, {})]


@pytest.mark.parametrize(
    ("status", "rc", "words"), [(404, 2, "not found"), (403, 1, "not allowed")]
)
def test_api_errors_print_one_line(api, capsys, status, rc, words) -> None:
    api.canned["rename_group"] = api_client.ApiError(status=status, body="", detail="nope")
    assert cli.main(["groups", "rename", "Trip", "x"]) == rc
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1
    assert words in err[0]


def test_uninstall_resolves_the_installed_app_by_title(api) -> None:
    api.canned["get_group"] = {
        "apps": {"installed": [{"install_id": "i1", "app_key": "k", "title": "Notes"}]}
    }
    assert cli.main(["groups", "uninstall-app", "Trip", "notes", "--yes"]) == 0
    assert api.calls[-1] == ("uninstall_group_app", GID, "i1", {})


def test_new_resolves_a_contact_name_to_its_handle(api, capsys) -> None:
    api.canned["search_contacts"] = [{"name": "Dana Lee", "handle": "dana@x.co"}]
    api.canned["create_group"] = {"group_id": GID, "created": True}
    assert cli.main(["groups", "new", "--name", "Plans", "dana", "+15551234567"]) == 0
    assert api.calls[-1] == (
        "create_group",
        {"people": ["dana@x.co", "+15551234567"], "name": "Plans"},
    )
    assert capsys.readouterr().out.strip() == GID


def test_read_mark_marks_through_the_newest_message(api) -> None:
    api.canned["list_group_messages"] = api_client.GroupMessagePage(
        [
            _message(created_at="2026-10-01T10:00:00+00:00"),
            _message(created_at="2026-10-02T10:00:00+00:00"),
        ]
    )
    api.canned["list_group_topics"] = [api_client.GroupTopicSummary(TID, "General", "2026-10-02")]
    assert cli.main(["groups", "read", "Trip", "--mark"]) == 0
    assert api.calls[-1] == (
        "set_group_read_mark",
        GID,
        TID,
        {"read_through": "2026-10-02T10:00:00+00:00"},
    )


def test_read_json_mark_failure_prints_nothing_and_fails(api, capsys) -> None:
    api.canned["list_group_messages"] = api_client.GroupMessagePage(
        [_message(created_at="2026-10-02T10:00:00+00:00")]
    )
    api.canned["list_group_topics"] = [api_client.GroupTopicSummary(TID, "General", "2026-10-02")]
    api.canned["set_group_read_mark"] = api_client.ApiError(status=500, body="", detail="boom")
    assert cli.main(["groups", "read", "Trip", "--json", "--mark"]) == 1
    assert capsys.readouterr().out == ""


def test_sharing_set_keeps_the_calendars_it_was_not_asked_to_change(api) -> None:
    api.canned["get_group_sharing"] = {
        "available_calendars": [
            {"calendar_id": "c1", "summary": "Work"},
            {"calendar_id": "c2", "summary": "Home"},
        ],
        "available_levels": ["none", "busy", "details"],
        "calendar": {"calendars": {"c1": "busy"}},
    }
    assert cli.main(["groups", "sharing", "Trip", "--set", "home=details"]) == 0
    assert api.calls[-1] == (
        "set_group_sharing",
        GID,
        {"calendars": {"c1": "busy", "c2": "details"}},
    )


def test_activity_names_the_recent_topics(api) -> None:
    api.canned["group_activity"] = []
    api.canned["list_group_topics"] = [api_client.GroupTopicSummary(TID, "General", "2026-10-02")]
    assert cli.main(["groups", "activity", "--since", "2026-10-01T00:00:00+00:00"]) == 0
    assert api.calls[1:] == [
        ("group_activity", {"since": "2026-10-01T00:00:00+00:00", "topic_ids": [TID]}),
    ]


def test_sharing_default_drops_the_explicit_choice(api) -> None:
    api.canned["get_group_sharing"] = {
        "available_calendars": [{"calendar_id": "c1", "summary": "Work"}],
        "available_levels": ["none", "busy"],
        "calendar": {"calendars": {"c1": "busy"}},
    }
    assert cli.main(["groups", "sharing", "Trip", "--set", "Work=default"]) == 0
    assert api.calls[-1] == ("set_group_sharing", GID, {"calendars": {}})


def test_download_to_an_unwritable_path_is_one_line(api, tmp_path, capsys) -> None:
    api.canned["get_group_attachment"] = b"x"
    assert cli.main(["groups", "download", "Trip", MID, "--out", str(tmp_path / "no" / "f")]) == 2
    assert "couldn't write" in capsys.readouterr().err
