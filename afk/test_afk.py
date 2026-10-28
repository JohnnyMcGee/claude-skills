import fnmatch
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import tomllib
import unittest
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path

import afk


class FakeRun:
    """Stands in for the run() seam: records every command and replays scripted stdout.

    Responses are keyed by a command prefix; the longest matching prefix wins. Like subprocess, a missing cwd fails.
    """

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []
        self.inputs = []
        self.cwds = []

    def __call__(self, cmd, input=None, cwd=None):
        self.calls.append(list(cmd))
        self.inputs.append(input)
        self.cwds.append(cwd and str(cwd))
        if cwd is not None and not Path(cwd).is_dir():
            raise FileNotFoundError(2, "No such file or directory", cwd)
        matches = [p for p in self.responses if tuple(cmd[: len(p)]) == p]
        if not matches:
            return ""
        response = self.responses[max(matches, key=len)]
        return response(cmd) if callable(response) else response

    def find(self, *prefix):
        return [c for c in self.calls if tuple(c[: len(prefix)]) == prefix]


def git(cmd):
    """Plain git: `worktree add` and `worktree remove` create and delete the worktree's dir, as git does."""
    if cmd[3:5] == ["worktree", "add"]:
        Path(cmd[-2]).mkdir(parents=True)
    elif cmd[3:5] == ["worktree", "remove"]:
        shutil.rmtree(cmd[-1])
    return ""


def pr_view(state, url="https://github.com/acme/widgets/pull/42", ready=False, created=None):
    """What `gh pr view <branch> --json state,url,labels,createdAt` prints for a PR in `state`, labelled by /open-pr
    if ready and opened at clock time `created` (by default, well after any worker started)."""
    labels = [{"name": "Ready for Review"}] if ready else []
    opened = datetime.fromtimestamp(created if created is not None else 2_000_000_000, timezone.utc)
    return json.dumps({"state": state, "url": url, "labels": labels, "createdAt": opened.strftime("%Y-%m-%dT%H:%M:%SZ")})


def no_pr(cmd):
    """What `gh pr view <branch>` does when the branch has no PR."""
    raise subprocess.CalledProcessError(1, cmd, stderr=f'no pull requests found for branch "{cmd[3]}"')


def model_response(transcript, tokens, at):
    """Append a model response using `tokens` at clock time `at` to a transcript, as Claude Code writes one: a line per
    content block, each repeating the response's usage, then the user line with the result of the tool it called."""
    transcript.parent.mkdir(parents=True, exist_ok=True)
    n = transcript.read_text().count("\n") if transcript.exists() else 0
    usage = {"input_tokens": 1, "cache_creation_input_tokens": tokens // 4, "cache_read_input_tokens": tokens // 2,
             "output_tokens": tokens - 1 - tokens // 4 - tokens // 2}
    timestamp = datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    entry = {"type": "assistant", "timestamp": timestamp, "requestId": f"req_{n}",
             "message": {"id": f"msg_{transcript.stem}_{n}", "role": "assistant", "usage": usage}}
    result = {"type": "user", "timestamp": timestamp,
              "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"toolu_{n}", "content": "ok"}]}}
    with open(transcript, "a") as file:
        for block in ({"type": "thinking"}, {"type": "tool_use", "id": f"toolu_{n}", "name": "Bash"}):
            file.write(json.dumps({**entry, "message": {**entry["message"], "content": [block]}}) + "\n")
        file.write(json.dumps(result) + "\n")


class AfkTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.state_home = self.tmp / "state"
        self.repo = self.tmp / "repo"
        self.scratch = self.repo / ".scratch" / "widgets"
        (self.scratch / "issues").mkdir(parents=True)
        (self.scratch / "spec.md").write_text("# Widgets spec\n")
        self.run_fake = FakeRun(
            {
                ("tmux", "display-message"): "work\n",
                ("tmux", "show-options"): "widgets\n",
                ("git", "-C"): git,
                ("tmux", "new-window"): "%5\n",
                ("tmux", "display-message", "-p", "-t"): "claude\n",  # the pane's foreground command
                ("gh", "pr", "view"): pr_view("OPEN"),  # the worker's branch has an open PR
            }
        )
        self.env = {"XDG_STATE_HOME": str(self.state_home), "HOME": str(self.tmp)}
        self.now = 1_000_000.0

    def afk(self, *argv, stdin="", env=None):
        out = io.StringIO()
        code = afk.main(
            list(argv),
            run=self.run_fake,
            env={**self.env, **(env or {})},
            stdin=io.StringIO(stdin),
            stdout=out,
            clock=lambda: self.now,
        )
        self.assertEqual(code, 0, out.getvalue())
        return out.getvalue()

    def ticket(self, name, title, status="ready-for-agent", blocked_by="None — can start immediately", type=None):
        number = name.split("-", 1)[0]
        (self.scratch / "issues" / f"{name}.md").write_text(
            f"# {number} — {title}\n\n"
            f"**What to build:** {title}.\n\n"
            f"**Blocked by:** {blocked_by}\n\n"
            f"**Status:** {status}\n\n"
            + (f"**Type:** {type}\n\n" if type else "")
            + "- [ ] It works\n"
        )

    @property
    def project_dir(self):
        return self.state_home / "afk" / "widgets"

    def clone(self, path, remote):
        (path / ".git").mkdir(parents=True)
        self.run_fake.responses[("git", "-C", str(path), "remote", "-v")] = f"origin\t{remote} (fetch)\norigin\t{remote} (push)\n"
        return path

    def repo_config(self, frontmatter, name="afk.md", body="# AFK\n\nRun `npm run dev`.\n"):
        """Write the repo's AFK config doc: TOML frontmatter between +++ lines, then prose."""
        path = self.repo / "docs" / "agents" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"+++\n{frontmatter.strip()}\n+++\n\n{body}")


class InitTest(AfkTestCase):
    def test_init_creates_project_state_and_tags_tmux_session(self):
        self.afk("init", str(self.scratch))

        config = tomllib.loads((self.project_dir / "config.toml").read_text())
        self.assertEqual(config["spec"], str(self.scratch))
        self.assertEqual(config["tracker"], "local")
        self.assertEqual(config["repo"], str(self.repo))
        self.assertEqual(config["session"], "work")
        self.assertEqual(
            self.run_fake.find("tmux", "set-option"),
            [["tmux", "set-option", "-t", "work", "@afk_project", "widgets"]],
        )


class LandingInitTest(AfkTestCase):
    def config(self):
        return tomllib.loads((self.project_dir / "config.toml").read_text())

    def test_init_with_local_landing_records_the_mode_and_target(self):
        self.afk("init", str(self.scratch), "--land", "local", "--target", "widgets-v2")

        self.assertEqual(self.config()["landing"], {"mode": "local", "target": "widgets-v2"})

    def test_init_without_flags_lands_by_pr_as_before(self):
        self.afk("init", str(self.scratch))

        self.assertNotIn("landing", self.config())

    def test_an_unknown_landing_mode_fails_before_anything_is_written(self):
        out = io.StringIO()

        code = afk.main(["init", str(self.scratch), "--land", "yolo"], run=self.run_fake, env=self.env,
                        stdin=io.StringIO(), stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("unknown landing mode 'yolo'", out.getvalue())
        self.assertFalse(self.project_dir.exists())
        self.assertEqual(self.run_fake.find("tmux", "set-option"), [])

    def test_reinit_without_arguments_keeps_the_landing(self):
        self.afk("init", str(self.scratch), "--land", "local", "--target", "widgets-v2")

        self.afk("init")

        self.assertEqual(self.config()["landing"], {"mode": "local", "target": "widgets-v2"})

    def test_reinit_with_a_new_target_keeps_local_landing(self):
        self.afk("init", str(self.scratch), "--land", "local", "--target", "widgets-v2")

        self.afk("init", "--target", "widgets-v3")

        self.assertEqual(self.config()["landing"], {"mode": "local", "target": "widgets-v3"})

    def test_reinit_with_pr_landing_drops_the_landing(self):
        self.afk("init", str(self.scratch), "--land", "local", "--target", "widgets-v2")

        self.afk("init", "--land", "pr")

        self.assertNotIn("landing", self.config())

    def test_a_target_without_local_landing_fails_before_anything_is_written(self):
        out = io.StringIO()

        code = afk.main(["init", str(self.scratch), "--target", "widgets-v2"], run=self.run_fake, env=self.env,
                        stdin=io.StringIO(), stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("--target needs --land local", out.getvalue())
        self.assertFalse(self.project_dir.exists())

    def test_local_landing_warns_when_the_repo_has_no_reviewers(self):
        self.repo_config('base = "main"')

        output = self.afk("init", str(self.scratch), "--land", "local")

        self.assertIn(f"warning: no reviewers in {self.repo}", output)

    def test_local_landing_is_quiet_about_a_repo_with_reviewers(self):
        self.repo_config('reviewers = ["copilot -p \'review git diff {{target}}...HEAD\'"]')

        output = self.afk("init", str(self.scratch), "--land", "local")

        self.assertNotIn("reviewers", output)

    def test_pr_landing_does_not_ask_for_reviewers(self):
        self.repo_config('base = "main"')

        output = self.afk("init", str(self.scratch))

        self.assertNotIn("reviewers", output)

    def test_pr_landing_does_not_read_the_repo_config(self):
        self.repo_config('base = "main"')
        self.repo_config("base = ", name="afk.local.md")

        output = self.afk("init", str(self.scratch))

        self.assertIn("bound to tmux session", output)

    def test_view_shows_the_landing(self):
        self.afk("init", str(self.scratch), "--land", "local", "--target", "widgets-v2")

        self.assertEqual(tomllib.loads(self.afk("view"))["landing"], {"mode": "local", "target": "widgets-v2"})


class ViewTest(AfkTestCase):
    def test_view_shows_the_current_project_config_and_its_path(self):
        self.afk("init", str(self.scratch))
        path = self.project_dir / "config.toml"
        with path.open("a") as config:
            config.write("\n[limits]\nmax_workers = 5\n")

        out = self.afk("view")

        self.assertEqual(out.splitlines()[0], f"# {path}")
        self.assertEqual(tomllib.loads(out), tomllib.loads(path.read_text()))


class GithubTestCase(AfkTestCase):
    """A project whose spec is GitHub issue acme/widgets#2 and whose tickets are its sub-issues, all via faked `gh`."""

    SPEC = "https://github.com/acme/widgets/issues/2"

    def setUp(self):
        super().setUp()
        self.issues = {}
        self.native_blockers = {}
        self.run_fake.responses[("git", "rev-parse", "--show-toplevel")] = f"{self.repo}\n"
        self.run_fake.responses[("tmux", "show-options")] = "widgets-2\n"
        self.run_fake.responses[("gh", "api")] = self.gh_api

    @staticmethod
    def key(repo, number):
        """How the fake files an issue: by number in the spec's repo, else by `name#number`, as afk names tickets."""
        number, repo = int(number), repo.lower()  # GitHub matches owner and repo names in any case
        return number if repo == "acme/widgets" else f"{repo.split('/')[1]}#{number}"

    def key_of(self, path):
        _, owner, name, _, number, *_ = path.split("/")
        return self.key(f"{owner}/{name}", number)

    def gh_api(self, cmd):
        path = next(arg for arg in cmd[2:] if arg.startswith("repos/")).lower()
        if path == "repos/acme/widgets/issues/2/sub_issues":
            result = list(self.issues.values())
        elif path.endswith("/dependencies/blocked_by"):
            result = [self.issues[k] for k in self.native_blockers.get(self.key_of(path), [])]
        else:
            result = self.issues[self.key_of(path)]
        return json.dumps([result] if "--slurp" in cmd else result)  # --slurp wraps each page in one array

    def issue(self, number, title, state="open", assignees=(), body="", repo="acme/widgets"):
        self.issues[self.key(repo, number)] = {
            "id": (1000 if repo == "acme/widgets" else 5000) + number,
            "number": number,
            "title": title,
            "state": state,
            "assignees": [{"login": login} for login in assignees],
            "body": body,
            "html_url": f"https://github.com/{repo}/issues/{number}",
            "repository_url": f"https://api.github.com/repos/{repo}",
        }

    @property
    def project_dir(self):
        return self.state_home / "afk" / "widgets-2"


class GithubInitTest(GithubTestCase):
    def test_init_from_a_spec_issue_url_records_the_github_tracker_and_the_local_checkout(self):
        self.afk("init", self.SPEC)

        config = tomllib.loads((self.project_dir / "config.toml").read_text())
        self.assertEqual(config["spec"], self.SPEC)
        self.assertEqual(config["tracker"], "github")
        self.assertEqual(config["repo"], str(self.repo))
        self.assertEqual(
            self.run_fake.find("tmux", "set-option"),
            [["tmux", "set-option", "-t", "work", "@afk_project", "widgets-2"]],
        )


class IntegrationBranchTest(GithubTestCase):
    """init in local mode creates a missing integration branch from each clone's base, under the same name."""

    def missing(self, clone, branch):
        def rev_parse(cmd):
            if cmd[-1] == f"{branch}^{{commit}}":
                raise subprocess.CalledProcessError(1, cmd)
            return ""

        self.run_fake.responses[("git", "-C", str(clone), "rev-parse")] = rev_parse

    def branches_created(self):
        return [(c[2], c[4:]) for c in self.run_fake.find("git", "-C") if c[3] == "branch"]

    def test_a_missing_integration_branch_is_created_from_base_in_every_clone_and_an_existing_one_left_alone(self):
        api = self.clone(self.repo / "api", "https://github.com/acme/api")
        web = self.clone(self.repo / "web", "https://github.com/acme/web")
        (api / "docs" / "agents").mkdir(parents=True)
        (api / "docs" / "agents" / "afk.md").write_text('+++\nbase = "develop"\n+++\n')
        self.issue(4, "Widget endpoints", repo="acme/api")
        self.issue(5, "Widget page", repo="acme/web")
        self.missing(self.repo, "widgets-v2")
        self.missing(api, "widgets-v2")

        self.afk("init", self.SPEC, "--land", "local", "--target", "widgets-v2")

        self.assertEqual(self.branches_created(), [(str(self.repo), ["widgets-v2", "main"]), (str(api), ["widgets-v2", "develop"])])

    def test_a_project_landing_on_base_creates_no_branch(self):
        self.missing(self.repo, "main")

        self.afk("init", self.SPEC, "--land", "local")
        self.afk("init", self.SPEC, "--land", "local", "--target", "main")

        self.assertEqual(self.branches_created(), [])


class InitReposTest(GithubTestCase):
    """init finds the clones of the repos the sub-issues live in, and re-running it refreshes the project."""

    def config(self):
        return tomllib.loads((self.project_dir / "config.toml").read_text())

    def test_clones_are_found_among_submodules_subdirectories_and_siblings_by_remote(self):
        self.repo.mkdir(exist_ok=True)
        (self.repo / ".gitmodules").write_text('[submodule "web"]\n\tpath = apps/web\n\turl = git@github.com:acme/web.git\n')
        web = self.clone(self.repo / "apps" / "web", "git@github.com:ACME/web.git")
        api = self.clone(self.repo / "api", "https://github.com/acme/api")
        docs = self.clone(self.tmp / "docs-checkout", "git@github.com:acme/docs.git")
        self.clone(self.tmp / "unrelated", "git@github.com:acme/unrelated.git")
        self.issue(4, "Widget page", repo="acme/web")
        self.issue(5, "Widget endpoints", repo="acme/api")
        self.issue(6, "Widget docs", body="Repo: acme/docs\n")

        output = self.afk("init", self.SPEC)

        self.assertEqual(self.config()["repos"], {"acme/web": str(web), "acme/api": str(api), "acme/docs": str(docs)})
        self.assertNotIn("no local clone", output)
        self.afk("start", "web#4")
        [add] = [c for c in self.run_fake.find("git", "-C") if c[3:5] == ["worktree", "add"]]
        self.assertEqual(add[2], str(web))

    def test_a_repo_without_a_clone_is_named_so_the_user_can_add_it(self):
        self.issue(4, "Widget page", repo="acme/web")

        output = self.afk("init", self.SPEC)

        self.assertNotIn("repos", self.config())
        self.assertIn("no local clone of acme/web", output)

    def test_local_landing_names_only_the_repos_with_no_reviewers(self):
        self.repo_config('reviewers = ["coderabbit review --plain --base {{target}}"]')
        web = self.clone(self.tmp / "web", "git@github.com:acme/web.git")
        api = self.clone(self.tmp / "api", "git@github.com:acme/api.git")
        (web / "docs" / "agents").mkdir(parents=True)
        (web / "docs" / "agents" / "afk.md").write_text('+++\nbase = "main"\n+++\n')
        self.issue(4, "Widget page", repo="acme/web")
        self.issue(5, "Widget endpoints", repo="acme/api")

        output = self.afk("init", self.SPEC, "--land", "local")

        [warning] = [line for line in output.splitlines() if "reviewers" in line]
        self.assertIn("acme/web", warning)
        self.assertIn("acme/api", warning)
        self.assertNotIn(str(self.repo), warning)

    def test_local_landing_names_the_spec_repo_by_owner_and_name(self):
        self.issue(4, "Widget page")

        output = self.afk("init", self.SPEC, "--land", "local")

        self.assertIn("warning: no reviewers in acme/widgets,", output)

    def test_reinit_without_a_spec_refreshes_repos_and_keeps_limits_overrides_and_hand_added_repos(self):
        self.afk("init", self.SPEC)
        mine = self.tmp / "mine"
        mine.mkdir()
        with open(self.project_dir / "config.toml", "a") as config:
            config.write(f'\n[limits]\nmax_workers = 5\n\n[overrides.task_types.frontend]\nmodel = "haiku"\n'
                         f'\n[repos]\n"acme/mine" = "{mine}"\n"acme/gone" = "{self.tmp / 'gone'}"\n')
        web = self.clone(self.tmp / "web", "git@github.com:acme/web.git")
        self.issue(4, "Widget page", repo="acme/web")

        self.afk("init")

        config = self.config()
        self.assertEqual(config["spec"], self.SPEC)
        self.assertEqual(config["limits"], {"max_workers": 5})
        self.assertEqual(config["overrides"], {"task_types": {"frontend": {"model": "haiku"}}})
        self.assertEqual(config["repos"], {"acme/mine": str(mine), "acme/web": str(web)})


class GithubFrontierTest(GithubTestCase):
    def test_frontier_lists_open_sub_issues_nobody_has_claimed(self):
        self.issue(3, "Widget schema", state="closed")
        self.issue(4, "Widget API")
        self.issue(5, "Widget UI", assignees=["someone-else"])
        self.issue(6, "Widget export")
        self.afk("init", self.SPEC)

        output = self.afk("frontier")

        self.assertEqual(output.splitlines(), ["4  Widget API", "6  Widget export"])

    def test_frontier_skips_sub_issues_with_an_open_native_blocker(self):
        self.issue(3, "Widget schema", state="closed")
        self.issue(4, "Widget API")
        self.issue(5, "Widget UI")
        self.issue(6, "Widget search")
        self.native_blockers = {5: [3], 6: [3, 4]}
        self.afk("init", self.SPEC)

        output = self.afk("frontier")

        self.assertEqual(output.splitlines(), ["4  Widget API", "5  Widget UI"])

    def test_without_native_blockers_the_body_blocked_by_section_is_used(self):
        self.issue(3, "Widget schema", state="closed")
        self.issue(4, "Widget API", body="## What to build\n\nNeeds 2 endpoints.\n\n## Blocked by\n\n- #3\n")
        self.issue(5, "Widget UI", body="## Blocked by\n\n- #3\n- #4\n\n## Notes\n\nSee #9.\n")
        self.issue(6, "Widget export", body="## Blocked by\n\nNone — can start immediately\n")
        self.afk("init", self.SPEC)

        output = self.afk("frontier")

        self.assertEqual(output.splitlines(), ["4  Widget API", "6  Widget export"])

    def test_the_spec_urls_casing_of_owner_and_repo_does_not_matter(self):
        self.issue(4, "Widget API")
        self.issue(4, "Widget page", repo="acme/web")
        self.afk("init", "https://github.com/ACME/widgets/issues/2")

        output = self.afk("frontier")

        self.assertEqual(output.splitlines(), ["4  Widget API", "web#4  Widget page  (acme/web)"])

    def test_a_sub_issue_from_another_owner_is_refused(self):
        self.issue(4, "Widget API")
        self.issue(7, "Upstream fix", repo="upstream/lib")
        self.afk("init", self.SPEC)

        out = io.StringIO()
        code = afk.main(["frontier"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out)

        self.assertNotEqual(code, 0)
        self.assertIn("upstream/lib#7 is outside acme", out.getvalue())

    def test_a_blocked_by_ref_is_read_in_the_issues_own_repo_unless_it_names_one(self):
        self.issue(3, "Widget schema", state="closed")
        self.issue(3, "Widget styles", repo="acme/web")
        self.issue(4, "Widget API", body="## Blocked by\n\n- acme/web#3\n")
        self.issue(5, "Widget page", repo="acme/web", body="## Blocked by\n\n- #3\n")
        self.issue(6, "Widget tour", repo="acme/web", body="## Blocked by\n\n- acme/widgets#3\n")
        self.afk("init", self.SPEC)

        output = self.afk("frontier")

        self.assertEqual(output.splitlines(), ["web#3  Widget styles  (acme/web)", "web#6  Widget tour  (acme/web)"])

    def test_a_repo_line_resolves_the_tickets_repo_and_its_absence_leaves_it_unresolved(self):
        self.issue(4, "Widget API", body="## What to build\n\nThe API.\n\n**Repo:** acme/widgets-api\n")
        self.issue(5, "Widget UI", body="Repo: acme/widgets-web\n")
        self.issue(6, "Widget export", body="## What to build\n\nExport to the repo's CSV format.\n")
        self.afk("init", self.SPEC)

        output = self.afk("frontier")

        self.assertEqual(
            output.splitlines(),
            ["4  Widget API  (acme/widgets-api)", "5  Widget UI  (acme/widgets-web)", "6  Widget export"],
        )

    def test_a_sub_issue_from_another_repo_is_named_by_repo_and_number_and_checked_against_its_own_repo(self):
        self.issue(3, "Widget schema")
        self.issue(4, "Widget API")
        self.issue(4, "Widget page", repo="acme/web")  # shares its number with the spec repo's #4
        self.native_blockers = {4: [3]}
        self.afk("init", self.SPEC)

        output = self.afk("frontier")

        self.assertEqual(output.splitlines(), ["3  Widget schema", "web#4  Widget page  (acme/web)"])


class GithubNextTest(GithubTestCase):
    def test_each_candidate_is_resolved_against_its_own_repos_task_types(self):
        api = self.tmp / "api"
        (api / "docs" / "agents").mkdir(parents=True)
        (api / "docs" / "agents" / "afk.md").write_text(
            '+++\ndefault_type = "service"\n[task_types.service]\nmodel = "haiku"\n+++\n'
        )
        self.repo_config(TASK_TYPES)
        self.issue(4, "Widget UI", body="Repo: acme/widgets\nType: frontend\n")
        self.issue(5, "Widget endpoints", body="Repo: acme/widgets-api\n")
        self.issue(6, "Widget app", body="Repo: acme/widgets-mobile\n")
        self.afk("init", self.SPEC)
        with open(self.project_dir / "config.toml", "a") as config:
            config.write(f'\n[repos]\n"acme/widgets-api" = "{api}"\n')

        tickets = {t["id"]: t for t in json.loads(self.afk("next"))["tickets"]}

        pick = lambda t: {k: t[k] for k in ("repo", "clone", "type", "model")}
        self.assertEqual(pick(tickets["4"]), {"repo": "acme/widgets", "clone": str(self.repo), "type": "frontend", "model": "sonnet"})
        self.assertEqual(pick(tickets["5"]), {"repo": "acme/widgets-api", "clone": str(api), "type": "service", "model": "haiku"})
        self.assertEqual(pick(tickets["6"]), {"repo": "acme/widgets-mobile", "clone": None, "type": None, "model": None})


# A ticket spanning two repos, as the parts /afk next proposes splitting it into.
SPLIT_PARTS = [
    {"repo": "acme/api", "title": "Export endpoint", "body": "Serve widgets as CSV."},
    {"repo": "acme/web", "title": "Export button", "body": "Download the CSV."},
]


class GithubSplitTest(GithubTestCase):
    """Splitting on GitHub, against a fake that applies the writes afk makes, so their effect shows in the frontier."""

    def setUp(self):
        super().setUp()
        self.not_sub_issues = set()
        self.run_fake.responses[("gh", "issue", "close")] = self.gh_issue_close
        self.issue(3, "Widget schema", state="closed")
        self.issue(5, "Widget export", body="## What to build\n\nExport.\n\n## Blocked by\n\n- #3\n")
        self.issue(6, "Widget report")
        self.issue(7, "Widget audit", body="## Blocked by\n\n- #5\n")
        self.native_blockers = {6: [5]}
        self.afk("init", self.SPEC)

    def gh_api(self, cmd):
        path = next(arg for arg in cmd[2:] if arg.startswith("repos/")).lower()
        method = cmd[cmd.index("-X") + 1] if "-X" in cmd else "GET"
        fields = dict(cmd[i + 1].split("=", 1) for i, arg in enumerate(cmd) if arg in ("-f", "-F"))
        by_id = lambda id: next(n for n, issue in self.issues.items() if issue["id"] == int(id))
        number = lambda: self.key_of(path)
        if (method, path) == ("POST", "repos/acme/widgets/issues"):
            created = max(n for n in self.issues if isinstance(n, int)) + 1
            self.issue(created, fields["title"], body=fields["body"])
            self.not_sub_issues.add(created)
            return json.dumps(self.issues[created])
        if (method, path) == ("POST", "repos/acme/widgets/issues/2/sub_issues"):
            self.not_sub_issues.discard(by_id(fields["sub_issue_id"]))
            return "{}"
        if method == "POST" and path.endswith("/dependencies/blocked_by"):
            self.native_blockers.setdefault(number(), []).append(by_id(fields["issue_id"]))
            return "{}"
        if method == "PATCH":
            self.issues[number()]["body"] = fields["body"]
            return "{}"
        if path == "repos/acme/widgets/issues/2/sub_issues":
            return json.dumps([[i for n, i in self.issues.items() if n not in self.not_sub_issues]])
        if path.endswith("/dependencies/blocking"):
            return json.dumps([[self.issues[d] for d, blockers in self.native_blockers.items() if number() in blockers]])
        return super().gh_api(cmd)

    def gh_issue_close(self, cmd):
        self.issues[self.key(cmd[cmd.index("--repo") + 1], cmd[3])]["state"] = "closed"
        return ""

    def test_a_sub_issue_from_another_repo_is_closed_there_and_its_dependents_rewired_in_their_own_repo(self):
        self.issue(3, "Widget styles", repo="acme/web")
        self.issue(5, "Widget page", repo="acme/web")
        self.issue(6, "Widget tour", repo="acme/web")
        self.native_blockers = {"web#5": ["web#3"], "web#6": ["web#5"]}

        self.afk("split", "web#5", stdin=json.dumps(SPLIT_PARTS))

        [close] = self.run_fake.find("gh", "issue", "close")
        self.assertEqual(close[3:6], ["5", "--repo", "acme/web"])
        self.assertEqual(self.native_blockers["web#6"], ["web#5", 8, 9])
        self.assertIn("## Blocked by\n\n- acme/web#3\n", self.issues[8]["body"])

    def test_blocked_by_refs_to_a_split_sub_issue_from_another_repo_are_rewired_relative_to_each_dependent(self):
        self.issue(5, "Widget page", repo="acme/web")
        self.issue(6, "Widget tour", repo="acme/web", body="## Blocked by\n\n- #5\n")
        self.issue(8, "Widget onboarding", body="## Blocked by\n\n- acme/web#5\n")

        self.afk("split", "web#5", stdin=json.dumps(SPLIT_PARTS))

        self.assertEqual(self.issues["web#6"]["body"], "## Blocked by\n\n- acme/widgets#9, acme/widgets#10\n")
        self.assertEqual(self.issues[8]["body"], "## Blocked by\n\n- #9, #10\n")
        self.assertEqual(self.issues[7]["body"], "## Blocked by\n\n- #5\n")  # the spec repo's own #5

    def test_a_blocked_by_ref_names_the_split_sub_issue_in_any_casing(self):
        self.issue(5, "Widget page", repo="acme/web")
        self.issue(8, "Widget onboarding", body="## Blocked by\n\n- ACME/Web#5\n")

        self.afk("split", "web#5", stdin=json.dumps(SPLIT_PARTS))

        self.assertEqual(self.issues[8]["body"], "## Blocked by\n\n- #9, #10\n")

    def test_repo_and_blocked_by_in_a_parts_body_do_not_override_its_own(self):
        self.issue(4, "Widget config")
        body = "Copied from #5.\n\nRepo: acme/other\n\n## Blocked by\n\n- #4\n"
        self.afk("split", "5", stdin=json.dumps([{"repo": "acme/api", "title": "Export endpoint", "body": body}]))

        self.assertIn("8  Export endpoint  (acme/api)", self.afk("frontier").splitlines())

    def test_split_closes_the_ticket_for_per_repo_sub_issues_that_its_dependents_wait_for(self):
        self.afk("split", "5", stdin=json.dumps(SPLIT_PARTS))

        self.assertEqual(self.afk("frontier").splitlines(), ["8  Export endpoint  (acme/api)", "9  Export button  (acme/web)"])
        [close] = self.run_fake.find("gh", "issue", "close")
        self.assertIn("Split into #8, #9", close[close.index("--comment") + 1])
        self.issues[3]["state"] = "open"  # the parts inherit the original's blockers
        self.assertEqual(self.afk("frontier").splitlines(), ["3  Widget schema"])
        self.issues[3]["state"] = "closed"
        self.issues[8]["state"] = self.issues[9]["state"] = "closed"
        self.assertEqual(self.afk("frontier").splitlines(), ["6  Widget report", "7  Widget audit"])


class GithubStartTest(GithubTestCase):
    def setUp(self):
        super().setUp()
        self.issue(4, "Widget API")
        self.afk("init", self.SPEC)

    def test_start_claims_the_ticket_by_assigning_it_before_setting_up_the_worker(self):
        self.afk("start", "4")

        claim = ["gh", "issue", "edit", "4", "--repo", "acme/widgets", "--add-assignee", "@me"]
        self.assertEqual(self.run_fake.find("gh", "issue", "edit"), [claim])
        [add] = self.run_fake.find("git", "-C", str(self.repo), "worktree", "add")
        self.assertLess(self.run_fake.calls.index(claim), self.run_fake.calls.index(add))

    def test_a_claimed_or_closed_ticket_cannot_be_started(self):
        self.issue(5, "Widget UI", assignees=["someone-else"])
        self.issue(6, "Widget export", state="closed")

        for number in ("5", "6"):
            out = io.StringIO()
            code = afk.main(["start", number], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out)

            self.assertNotEqual(code, 0)
            self.assertIn("not open", out.getvalue())
        self.assertEqual(self.run_fake.find("gh", "issue", "edit"), [])
        self.assertEqual(self.run_fake.find("git", "-C", str(self.repo), "worktree", "add"), [])

    def test_failed_start_releases_the_claim(self):
        def split_fails(cmd):
            raise subprocess.CalledProcessError(1, cmd, stderr="no space for new pane")

        self.run_fake.responses[("tmux", "split-window")] = split_fails
        code = afk.main(["start", "4"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=io.StringIO())

        self.assertNotEqual(code, 0)
        self.assertEqual(
            self.run_fake.find("gh", "issue", "edit"),
            [
                ["gh", "issue", "edit", "4", "--repo", "acme/widgets", "--add-assignee", "@me"],
                ["gh", "issue", "edit", "4", "--repo", "acme/widgets", "--remove-assignee", "@me"],
            ],
        )

    def test_a_sub_issue_from_another_repo_is_claimed_there_and_worked_on_in_its_clone(self):
        web = self.tmp / "web"
        with open(self.project_dir / "config.toml", "a") as config:
            config.write(f'\n[repos]\n"acme/web" = "{web}"\n')
        self.issue(4, "Widget page", repo="acme/web")

        self.afk("start", "web#4")

        self.assertEqual(
            self.run_fake.find("gh", "issue", "edit"),
            [["gh", "issue", "edit", "4", "--repo", "acme/web", "--add-assignee", "@me"]],
        )
        [add] = [c for c in self.run_fake.find("git", "-C") if c[3:5] == ["worktree", "add"]]
        self.assertEqual(add[2], str(web))
        [send] = self.run_fake.find("tmux", "send-keys")
        self.assertIn("https://github.com/acme/web/issues/4", shlex.split(send[4])[-1])

    def test_a_worker_whose_issue_is_closed_while_its_pr_is_open_is_cleaned_up_leaving_both_as_they_are(self):
        self.issue(7, "Widget spike", body="Type: hitl\n")
        self.run_fake.responses[("tmux", "split-window")] = "%6\n"
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%6")] = "bash\n"
        self.afk("start", "7")
        self.afk("tick")
        self.assertTrue((self.project_dir / "workers" / "7").exists())  # its PR is open and its issue too
        self.issues[7]["state"] = "closed"
        self.now += 60

        self.afk("tick")

        worktree = str(self.project_dir / "worktrees" / "7")
        self.assertEqual(self.run_fake.find("git", "-C", str(self.repo), "worktree", "remove"), [["git", "-C", str(self.repo), "worktree", "remove", "--force", worktree]])
        self.assertEqual(self.run_fake.find("git", "-C", str(self.repo), "branch", "-D"), [["git", "-C", str(self.repo), "branch", "-D", "afk/widgets-2-7"]])
        self.assertFalse((self.project_dir / "workers" / "7").exists())
        # Neither the closed issue nor the open PR is touched: only the claim at start edits GitHub.
        self.assertEqual(self.run_fake.find("gh", "issue", "edit"), [["gh", "issue", "edit", "7", "--repo", "acme/widgets", "--add-assignee", "@me"]])
        self.assertEqual({tuple(c[:3]) for c in self.run_fake.find("gh", "pr")}, {("gh", "pr", "view")})

    def test_worker_is_pointed_at_the_issue_url(self):
        self.afk("start", "4")

        [send] = self.run_fake.find("tmux", "send-keys")
        self.assertIn("https://github.com/acme/widgets/issues/4", shlex.split(send[4])[-1])

    def test_a_tickets_repo_line_picks_its_clone_and_the_spec_repo_is_the_projects_own(self):
        api = self.tmp / "api"
        with open(self.project_dir / "config.toml", "a") as config:
            config.write(f'\n[repos]\n"acme/widgets-api" = "{api}"\n')
        self.issue(5, "Widget endpoints", body="Repo: acme/widgets-api\n")
        self.issue(6, "Widget docs", body="Repo: acme/widgets\n")
        self.run_fake.responses[("tmux", "new-window")] = lambda cmd: "%5\n"

        self.afk("start", "5")
        self.afk("start", "6")

        self.assertEqual(
            [c[2] for c in self.run_fake.find("git", "-C") if c[3:5] == ["worktree", "add"]],
            [str(api), str(self.repo)],
        )


class GithubDropTest(GithubTestCase):
    def setUp(self):
        super().setUp()
        self.issue(4, "Widget API")
        self.afk("init", self.SPEC, "--land", "local")
        self.run_fake.responses[("tmux", "split-window")] = "%6\n"
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%6")] = "bash\n"
        self.afk("start", "4")
        self.run_fake.responses[("git", "-C", str(self.repo), "rev-list")] = "1\n"

    def test_a_dropped_workers_issue_is_unassigned_back_onto_the_frontier(self):
        self.afk("drop", "4", "--discard")

        self.assertEqual(self.run_fake.find("gh", "issue", "edit"), [
            ["gh", "issue", "edit", "4", "--repo", "acme/widgets", "--add-assignee", "@me"],
            ["gh", "issue", "edit", "4", "--repo", "acme/widgets", "--remove-assignee", "@me"],
        ])
        self.assertEqual(self.run_fake.find("git", "-C", str(self.repo), "branch", "-D"),
                         [["git", "-C", str(self.repo), "branch", "-D", "afk/widgets-2-4"]])
        self.assertFalse((self.project_dir / "workers" / "4").exists())

    def test_a_dropped_worker_whose_issue_is_closed_leaves_it_as_it_is(self):
        self.issues[4]["state"] = "closed"

        out = self.afk("drop", "4", "--discard")

        self.assertEqual(self.run_fake.find("gh", "issue", "edit"), [
            ["gh", "issue", "edit", "4", "--repo", "acme/widgets", "--add-assignee", "@me"],
        ])
        self.assertNotIn("frontier", out)
        self.assertFalse((self.project_dir / "workers" / "4").exists())


class GithubCompleteTest(GithubTestCase):
    def test_a_landed_ticket_is_closed_as_completed_naming_where_it_landed(self):
        self.issue(4, "Widget API")
        self.issue(9, "Widget page", repo="acme/web")
        tracker = afk.GithubTracker(self.run_fake, self.SPEC)

        tracker.complete(tracker.get("4"), landed=("3f2a9c1e", "widgets-v2"))
        tracker.complete(tracker.get("web#9"), landed=("8b7d6e5f", "main"))

        self.assertEqual(self.run_fake.find("gh", "issue", "close"), [
            ["gh", "issue", "close", "4", "--repo", "acme/widgets", "--reason", "completed",
             "--comment", "Landed in 3f2a9c1e on widgets-v2"],
            ["gh", "issue", "close", "9", "--repo", "acme/web", "--reason", "completed",
             "--comment", "Landed in 8b7d6e5f on main"],
        ])

    def test_a_merged_ticket_is_left_for_its_pr_to_close(self):
        self.issue(4, "Widget API")
        tracker = afk.GithubTracker(self.run_fake, self.SPEC)

        tracker.complete(tracker.get("4"))

        self.assertEqual(self.run_fake.find("gh", "issue", "close"), [])


class GithubCommentTest(GithubTestCase):
    def test_a_comment_is_posted_on_the_tickets_issue_in_its_own_repo(self):
        self.issue(4, "Widget API")
        self.issue(9, "Widget page", repo="acme/web")
        tracker = afk.GithubTracker(self.run_fake, self.SPEC)

        tracker.comment(tracker.get("4"), "Draft: https://github.com/acme/widgets/pull/12")
        tracker.comment(tracker.get("web#9"), "Abandoned: it ran past ticket_minutes")

        self.assertEqual(self.run_fake.find("gh", "issue", "comment"), [
            ["gh", "issue", "comment", "4", "--repo", "acme/widgets", "--body", "Draft: https://github.com/acme/widgets/pull/12"],
            ["gh", "issue", "comment", "9", "--repo", "acme/web", "--body", "Abandoned: it ran past ticket_minutes"],
        ])


class FakeLinear:
    """The `linear` CLI: it answers afk's GraphQL reads from the issues and blocked-by relations it holds, and
    applies the issue updates afk makes, so their effect shows in later reads."""

    def __init__(self):
        self.issues = {}  # identifier -> the issue's fields
        self.blocked_by = {}  # identifier -> the identifiers of the issues that block it

    def issue(self, identifier, title, state="unstarted", assignee=None, description="", child=True, parent=True):
        """A Linear issue in a state of the given type; a child is in the project, and a sub-issue of ENG-1 unless it
        has no parent."""
        self.issues[identifier] = {"title": title, "state": state, "assignee": assignee, "description": description,
                                   "child": child, "parent": child and parent}

    def node(self, identifier):
        issue = self.issues[identifier]
        blockers = [{"type": "blocks", "issue": {"identifier": b, "state": {"type": self.issues[b]["state"]}}}
                    for b in self.blocked_by.get(identifier, [])]
        related = [{"type": "related", "issue": {"identifier": "ENG-99", "state": {"type": "started"}}}]
        return {
            "identifier": identifier, "title": issue["title"], "description": issue["description"] or None,
            "url": f"https://linear.app/acme/issue/{identifier}/{afk.slug(issue['title'])}",
            "branchName": f"me/{identifier.lower()}-{afk.slug(issue['title'])}",
            "state": {"type": issue["state"]},
            "assignee": issue["assignee"] and {"displayName": issue["assignee"]},
            "inverseRelations": {"nodes": blockers + related},
            "id": f"uuid-{identifier}", "team": {"id": f"team-{identifier.split('-')[0]}"},
            "parent": {"id": "uuid-ENG-1"} if issue["parent"] else None,
            "project": {"id": "uuid-8f3c2a1b9d0e"} if issue["child"] else None,
            "relations": {"nodes": [{"type": "blocks", "relatedIssue": {"identifier": d}}
                                    for d, blockers in self.blocked_by.items() if identifier in blockers]
                          + [{"type": "related", "relatedIssue": {"identifier": "ENG-99"}}]},
        }

    def api(self, cmd):
        document, variables = cmd[2], json.loads(cmd[cmd.index("--variables-json") + 1])
        if "viewer" in document:
            return json.dumps({"data": {"viewer": {"displayName": "Me"}}})  # whoever `--assignee @me` assigns
        # As the CLI does for any GraphQL error: the errors on stdout, exit 3 for an entity not found and 1 for the rest.
        fail = lambda code, message: subprocess.CalledProcessError(
            code, cmd, output=json.dumps({"errors": [{"message": message}], "data": None}), stderr="")
        # Linear's documented complexity: 1 per object and 0.1 per field, a connection's nodes counted `first` times.
        # An issue as afk reads it is 3.6, and each of its relations 3.3.
        pages = [int(n) for n in re.findall(r"first:\s*(\d+)", document)]
        complexity = pages[0] * (3.6 + 3.3 * pages[1]) if len(pages) == 2 else 3.6 + 3.3 * pages[0]
        if complexity > 10000:
            raise fail(1, f"The query is too complex. Complexity: {int(complexity)}. Maximum allowed complexity: 10000.")
        listed = "children" in document and "parent" or "child"
        children = [self.node(i) for i, issue in self.issues.items() if issue[listed]]
        start = int(variables.get("after") or 0)
        page = {"nodes": children[start:start + pages[0]],
                "pageInfo": {"hasNextPage": start + pages[0] < len(children), "endCursor": str(start + pages[0])}}
        if "children" in document:
            if variables["id"] != "ENG-1":
                raise fail(3, "Entity not found: Issue")
            data = {"issue": {"children": page}}
        elif "project(" in document:
            if variables["id"] != "8f3c2a1b9d0e":
                raise fail(3, "Entity not found: Project")
            data = {"project": {"issues": page}}
        elif variables["id"] in self.issues:
            data = {"issue": self.node(variables["id"])}
        else:
            raise fail(3, "Entity not found: Issue")
        return json.dumps({"data": data})

    def update(self, cmd):
        issue = self.issues[cmd[3]]
        options = cmd[4:]
        if "--assignee" in options:
            issue["assignee"] = "Me" if options[options.index("--assignee") + 1] == "@me" else None
        if "--unassign" in options:
            issue["assignee"] = None
        if "--state" in options:
            issue["state"] = options[options.index("--state") + 1]
        return ""


class LinearTestCase(AfkTestCase):
    """A project whose spec is Linear issue ENG-1 and whose tickets are its sub-issues, all via a faked `linear` CLI."""

    SPEC = "https://linear.app/acme/issue/ENG-1/widgets-spec"
    PROJECT = "https://linear.app/acme/project/widgets-redesign-8f3c2a1b9d0e/overview"

    def setUp(self):
        super().setUp()
        self.linear = FakeLinear()
        self.issues, self.blocked_by = self.linear.issues, self.linear.blocked_by
        self.run_fake.responses[("git", "rev-parse", "--show-toplevel")] = f"{self.repo}\n"
        self.run_fake.responses[("tmux", "show-options")] = "eng1\n"
        self.run_fake.responses[("linear", "api")] = self.linear.api
        self.run_fake.responses[("linear", "issue", "update")] = self.linear.update

    def issue(self, *args, **kwargs):
        self.linear.issue(*args, **kwargs)

    def config(self):
        return tomllib.loads((self.project_dir / "config.toml").read_text())

    @property
    def project_dir(self):
        return self.state_home / "afk" / "eng1"


class LinearInitTest(LinearTestCase):
    def test_init_from_a_parent_issue_url_records_the_linear_tracker_and_the_local_checkout(self):
        output = self.afk("init", self.SPEC)

        config = self.config()
        self.assertEqual(config["spec"], self.SPEC)
        self.assertEqual(config["tracker"], "linear")
        self.assertEqual(config["repo"], str(self.repo))
        self.assertEqual(self.run_fake.find("tmux", "set-option"), [["tmux", "set-option", "-t", "work", "@afk_project", "eng1"]])
        # Named without the spec's identifier, so a worker's branch names only its own ticket to Linear's GitHub integration.
        self.assertIn("afk project 'eng1' bound to tmux session 'work'", output)

    def test_init_from_a_project_url_names_the_project_by_its_slug_and_takes_its_issues_as_tickets(self):
        self.run_fake.responses[("tmux", "show-options")] = "widgets-redesign-8f3c2a1b9d0e\n"
        self.issue("ENG-2", "Widget API")

        output = self.afk("init", self.PROJECT)

        project_dir = self.state_home / "afk" / "widgets-redesign-8f3c2a1b9d0e"
        self.assertEqual(tomllib.loads((project_dir / "config.toml").read_text())["tracker"], "linear")
        self.assertIn("afk project 'widgets-redesign-8f3c2a1b9d0e' bound", output)
        self.assertEqual(self.afk("frontier").splitlines(), ["ENG-2  Widget API"])

    def test_a_parent_issue_url_is_read_in_any_form_linear_gives_it(self):
        self.issue("ENG-2", "Widget API")
        for url in ("https://linear.app/acme/issue/ENG-1", "https://linear.app/acme/issue/eng-1/widgets-spec/",
                    "https://linear.app/acme/issue/ENG-1?view=sub-issues", "https://linear.app/acme/issue/ENG-1/widgets-spec#comment-4f2a"):
            with self.subTest(url):
                output = self.afk("init", url)

                self.assertIn("afk project 'eng1' bound", output)
                self.assertEqual(self.afk("frontier").splitlines(), ["ENG-2  Widget API"])  # the fake reads only ENG-1's

    def test_a_project_url_is_read_in_any_form_linear_gives_it(self):
        self.run_fake.responses[("tmux", "show-options")] = "widgets-redesign-8f3c2a1b9d0e\n"
        self.issue("ENG-2", "Widget API")
        for url in ("https://linear.app/acme/project/widgets-redesign-8f3c2a1b9d0e",
                    "https://linear.app/acme/project/widgets-redesign-8f3c2a1b9d0e/issues?layout=board",
                    "https://linear.app/acme/project/widgets-redesign-8f3c2a1b9d0e#overview"):
            with self.subTest(url):
                output = self.afk("init", url)

                self.assertIn("afk project 'widgets-redesign-8f3c2a1b9d0e' bound", output)
                self.assertEqual(self.afk("frontier").splitlines(), ["ENG-2  Widget API"])  # the fake reads only 8f3c2a1b9d0e's

    def test_reinit_without_a_spec_keeps_the_linear_project(self):
        self.afk("init", self.SPEC)
        self.issue("ENG-2", "Widget page", description="Repo: acme/web\n")
        web = self.clone(self.tmp / "web", "git@github.com:acme/web.git")

        self.afk("init")

        self.assertEqual({k: self.config()[k] for k in ("spec", "tracker", "repos")},
                         {"spec": self.SPEC, "tracker": "linear", "repos": {"acme/web": str(web)}})

    def test_a_linear_error_is_shown_and_nothing_is_written(self):
        out = io.StringIO()

        code = afk.main(["init", "https://linear.app/acme/issue/ENG-404"], run=self.run_fake, env=self.env,
                        stdin=io.StringIO(), stdout=out)

        self.assertEqual(code, 1)
        self.assertTrue(out.getvalue().rstrip().endswith("failed: Entity not found: Issue"), out.getvalue())
        self.assertEqual(self.run_fake.find("tmux", "set-option"), [])

    def test_init_maps_the_repos_tickets_name_on_their_repo_lines_to_clones(self):
        web = self.clone(self.tmp / "web", "git@github.com:acme/web.git")
        self.issue("ENG-2", "Widget page", description="## What to build\n\nThe page.\n\n**Repo:** acme/web\n")
        self.issue("ENG-3", "Widget endpoints", description="Repo: acme/api\n")

        output = self.afk("init", self.SPEC)

        self.assertEqual(self.config()["repos"], {"acme/web": str(web)})
        self.assertIn("no local clone of acme/api", output)


class LinearFrontierTest(LinearTestCase):
    def test_frontier_lists_open_unassigned_issues_whose_blockers_have_all_ended_with_their_repos(self):
        self.issue("ENG-2", "Widget schema", state="completed")
        self.issue("ENG-3", "Widget API", description="Repo: acme/api\n")
        self.issue("ENG-4", "Widget UI", assignee="Someone Else")
        self.issue("ENG-5", "Widget export", state="canceled")
        self.issue("ENG-6", "Widget search", description="Repo: acme/web\n")
        self.issue("ENG-7", "Widget audit")
        self.issue("OPS-8", "Upstream fix", state="started", child=False)
        self.blocked_by.update({"ENG-3": ["ENG-2", "ENG-5"], "ENG-6": ["ENG-3"], "ENG-7": ["OPS-8"]})
        self.afk("init", self.SPEC)

        output = self.afk("frontier")

        self.assertEqual(output.splitlines(), ["ENG-3  Widget API  (acme/api)"])
        self.issues["ENG-3"]["state"] = self.issues["OPS-8"]["state"] = "completed"
        self.assertEqual(self.afk("frontier").splitlines(), ["ENG-6  Widget search  (acme/web)", "ENG-7  Widget audit"])


    def test_a_spec_of_more_issues_than_a_page_holds_is_read_whole(self):
        for n in range(2, 302):
            self.issue(f"ENG-{n}", f"Widget part {n}")
        self.afk("init", self.SPEC)

        output = self.afk("frontier").splitlines()

        self.assertEqual((len(output), output[0], output[-1]), (300, "ENG-2  Widget part 2", "ENG-301  Widget part 301"))


class LinearNextTest(LinearTestCase):
    def test_each_candidate_takes_its_repo_and_clone_from_its_repo_line_and_its_type_from_its_type_line(self):
        api = self.clone(self.tmp / "api", "git@github.com:acme/api.git")
        (api / "docs" / "agents").mkdir(parents=True)
        (api / "docs" / "agents" / "afk.md").write_text('+++\ndefault_type = "service"\n[task_types.service]\nmodel = "haiku"\n+++\n')
        self.repo_config(TASK_TYPES)
        self.issue("ENG-2", "Widget endpoints", description="Repo: acme/api\n")
        self.issue("ENG-3", "Widget UI", description="Type: frontend\n")
        self.issue("ENG-4", "Widget app", description="Repo: acme/mobile\n")
        self.afk("init", self.SPEC)

        tickets = {t["id"]: t for t in json.loads(self.afk("next"))["tickets"]}

        pick = lambda t: {k: t[k] for k in ("repo", "clone", "type", "model", "path")}
        self.assertEqual(pick(tickets["ENG-2"]), {"repo": "acme/api", "clone": str(api), "type": "service", "model": "haiku",
                                                  "path": "https://linear.app/acme/issue/ENG-2/widget-endpoints"})
        self.assertEqual(pick(tickets["ENG-3"]), {"repo": None, "clone": str(self.repo), "type": "frontend", "model": "sonnet",
                                                  "path": "https://linear.app/acme/issue/ENG-3/widget-ui"})
        self.assertEqual(pick(tickets["ENG-4"]), {"repo": "acme/mobile", "clone": None, "type": None, "model": None,
                                                  "path": "https://linear.app/acme/issue/ENG-4/widget-app"})


class LinearSplitTest(LinearTestCase):
    """Splitting on Linear, against a fake that applies the issues and relations afk creates, so they show in the frontier."""

    def setUp(self):
        super().setUp()
        self.run_fake.responses[("linear", "api")] = self.linear_api
        self.run_fake.responses[("linear", "issue", "relation", "add")] = self.linear_relation_add
        self.issue("ENG-3", "Widget schema", state="completed")
        self.issue("ENG-5", "Widget export", description="## What to build\n\nExport.\n")
        self.issue("ENG-6", "Widget report")
        self.issue("ENG-7", "Widget audit")
        self.blocked_by.update({"ENG-5": ["ENG-3"], "ENG-6": ["ENG-5"], "ENG-7": ["ENG-5"]})
        self.afk("init", self.SPEC)

    def linear_api(self, cmd):
        document, variables = cmd[2], json.loads(cmd[cmd.index("--variables-json") + 1])
        if "issueCreate" not in document:
            return self.linear.api(cmd)
        fields = variables["input"]
        identifier = f"ENG-{max(int(i.split('-')[1]) for i in self.issues) + 1}"
        # In ENG's team and the project, it is one of the project's issues; under ENG-1 too, one of ENG-1's sub-issues.
        child = (fields.get("teamId"), fields.get("projectId")) == ("team-ENG", "uuid-8f3c2a1b9d0e")
        self.issue(identifier, fields["title"], description=fields["description"], child=child,
                   parent=fields.get("parentId") == "uuid-ENG-1")
        return json.dumps({"data": {"issueCreate": {"issue": {"identifier": identifier, "url": self.linear.node(identifier)["url"]}}}})

    def linear_relation_add(self, cmd):
        issue, relation, related = cmd[4:7]
        self.assertEqual(relation, "blocks")
        self.assertTrue({issue, related} <= set(self.issues), cmd)  # Linear refuses a relation to no issue
        self.blocked_by.setdefault(related, []).append(issue)
        return ""

    def split(self, ticket, parts=SPLIT_PARTS):
        return self.afk("split", ticket, stdin=json.dumps(parts))

    def test_split_closes_the_ticket_for_one_issue_per_part_in_the_spec(self):
        output = self.split("ENG-5")

        self.assertIn("split ENG-5 into ENG-8, ENG-9", output)
        self.assertEqual(self.afk("frontier").splitlines(), ["ENG-8  Export endpoint  (acme/api)", "ENG-9  Export button  (acme/web)"])
        self.assertIn("Serve widgets as CSV.", self.issues["ENG-8"]["description"])
        self.assertEqual(self.issues["ENG-5"]["state"], "canceled")
        [comment] = self.run_fake.find("linear", "issue", "comment", "add")
        # By URL, which Linear turns into a mention of the issue.
        self.assertEqual(comment[4:7], ["ENG-5", "--body", "Split into https://linear.app/acme/issue/ENG-8/export-endpoint, "
                                        "https://linear.app/acme/issue/ENG-9/export-button, one per repo."])

    def test_split_in_a_project_puts_the_parts_in_the_project_beside_a_ticket_with_no_parent(self):
        self.run_fake.responses[("tmux", "show-options")] = "widgets-redesign-8f3c2a1b9d0e\n"
        self.issue("ENG-5", "Widget export", parent=False)
        self.afk("init", self.PROJECT)

        self.split("ENG-5")

        self.assertEqual(self.afk("frontier").splitlines(), ["ENG-8  Export endpoint  (acme/api)", "ENG-9  Export button  (acme/web)"])
        self.assertEqual([self.issues[i]["parent"] for i in ("ENG-8", "ENG-9")], [False, False])

    def test_each_part_is_blocked_by_the_tickets_blockers_and_blocks_its_dependents(self):
        self.issue("OPS-4", "Upstream fix", state="started", child=False)
        self.blocked_by["ENG-5"].append("OPS-4")
        self.split("ENG-5")

        self.assertEqual(self.afk("frontier").splitlines(), [])
        self.issues["OPS-4"]["state"] = "completed"
        self.issues["ENG-3"]["state"] = "started"  # reopening either blocker blocks every part again
        self.assertEqual(self.afk("frontier").splitlines(), ["ENG-3  Widget schema"])
        self.issues["ENG-3"]["state"] = "completed"
        self.issues["ENG-8"]["state"] = "completed"
        self.assertEqual(self.afk("frontier").splitlines(), ["ENG-9  Export button  (acme/web)"])
        self.issues["ENG-9"]["state"] = "completed"
        self.assertEqual(self.afk("frontier").splitlines(), ["ENG-6  Widget report", "ENG-7  Widget audit"])

    def test_next_offers_each_part_in_its_own_repo(self):
        web = self.clone(self.tmp / "web", "git@github.com:acme/web.git")
        body = "Copied from ENG-5.\n\nRepo: acme/other\n"
        self.split("ENG-5", [SPLIT_PARTS[0], {**SPLIT_PARTS[1], "body": body}])
        self.afk("init")  # maps acme/web, now that a part names it

        tickets = {t["id"]: t for t in json.loads(self.afk("next"))["tickets"]}

        self.assertEqual({id: (t["title"], t["repo"]) for id, t in tickets.items()},
                         {"ENG-8": ("Export endpoint", "acme/api"), "ENG-9": ("Export button", "acme/web")})
        self.assertEqual(tickets["ENG-9"]["clone"], str(web))


class LinearStartTest(LinearTestCase):
    CLAIM = ["linear", "issue", "update", "ENG-2", "--assignee", "@me", "--no-input"]

    def setUp(self):
        super().setUp()
        self.issue("ENG-2", "Widget API")
        self.afk("init", self.SPEC)

    def test_start_claims_the_ticket_by_assigning_it_to_me_before_setting_up_the_worker(self):
        self.afk("start", "ENG-2")

        self.assertEqual(self.run_fake.find("linear", "issue", "update"), [self.CLAIM])
        [add] = self.run_fake.find("git", "-C", str(self.repo), "worktree", "add")
        self.assertLess(self.run_fake.calls.index(self.CLAIM), self.run_fake.calls.index(add))
        self.assertEqual(add[5:7], ["-b", "afk/eng1-ENG-2"])
        [send] = self.run_fake.find("tmux", "send-keys")
        self.assertIn("https://linear.app/acme/issue/ENG-2/widget-api", shlex.split(send[4])[-1])
        self.assertEqual(self.afk("frontier"), "")

    def test_status_and_the_dashboard_line_a_long_identifier_up_with_its_columns(self):
        self.issue("ENG-12345", "Widget search")
        self.run_fake.responses[("tmux", "new-window")] = lambda cmd: "%5\n"
        self.afk("start", "ENG-2")
        self.afk("start", "ENG-12345")

        for output in (self.afk("status"), self.afk("tick")):
            header, *rows = [line for line in output.splitlines() if line.startswith(("TICKET", "ENG-"))]
            with self.subTest(header):
                self.assertEqual([row.split()[:2] for row in rows], [["ENG-12345", "implement"], ["ENG-2", "implement"]])
                self.assertEqual({row.index("implement") for row in rows}, {header.index("PHASE")})

    def test_a_ticket_is_named_in_any_case(self):
        self.afk("start", "eng-2")

        self.assertEqual(self.run_fake.find("linear", "issue", "update"), [self.CLAIM])

    def test_an_assigned_or_ended_ticket_cannot_be_started(self):
        self.issue("ENG-3", "Widget UI", assignee="Someone Else")
        self.issue("ENG-4", "Widget export", state="completed")

        for identifier in ("ENG-3", "ENG-4"):
            out = io.StringIO()
            code = afk.main(["start", identifier], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out)

            self.assertNotEqual(code, 0)
            self.assertIn("not open", out.getvalue())
        self.assertEqual(self.run_fake.find("linear", "issue", "update"), [])

    def test_an_unknown_ticket_is_named_as_such(self):
        out = io.StringIO()
        code = afk.main(["start", "ENG-404"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out)

        self.assertNotEqual(code, 0)
        self.assertIn("no ticket 'ENG-404'", out.getvalue())

    def test_failed_start_releases_the_claim(self):
        def split_fails(cmd):
            raise subprocess.CalledProcessError(1, cmd, stderr="no space for new pane")

        self.run_fake.responses[("tmux", "split-window")] = split_fails
        code = afk.main(["start", "ENG-2"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=io.StringIO())

        self.assertNotEqual(code, 0)
        self.assertEqual(self.run_fake.find("linear", "issue", "update"),
                         [self.CLAIM, ["linear", "issue", "update", "ENG-2", "--unassign", "--no-input"]])
        self.assertEqual(self.afk("frontier").splitlines(), ["ENG-2  Widget API"])


class QueueTestCase(GithubTestCase):
    """The machine-wide queue, run from a session bound to no project: standalone GitHub issues via faked `gh`."""

    URL = "https://github.com/acme/widgets/issues/12"

    def setUp(self):
        super().setUp()
        self.run_fake.responses[("tmux", "show-options")] = "\n"  # no project here
        self.run_fake.responses[("gh", "api", "user")] = "me\n"
        self.clone(self.repo, "git@github.com:acme/widgets.git")
        self.repo_config('default_type = "feature"\n\n[task_types.feature]\nmodel = "sonnet"\neffort = "medium"\n')
        self.issue(12, "Widget export")
        self.linked = {}  # (issue's repo, number) -> [(branch's repo, name)], the branches GitHub links to an issue
        self.run_fake.responses[("gh", "api", "graphql")] = self.linked_branches
        self.run_fake.responses[("gh", "issue", "develop")] = self.develop
        self.remotes = {}  # (clone, branch) -> its commits beyond base on the clone's origin
        self.clones = {"acme/widgets": self.repo}  # where `gh issue develop`'s new branches show up on origin
        self.on_origin(self.repo)

    def on_origin(self, clone):
        """Answer `git ls-remote` and `git rev-list --count` in clone from the branches pushed to its origin."""
        def ls_remote(cmd):
            branch = cmd[-1].removeprefix("refs/heads/")
            return f"3f2a9c1\trefs/heads/{branch}\n" if (str(clone), branch) in self.remotes else ""
        def ahead(cmd):
            return f"{self.remotes[(str(clone), cmd[-1].partition('..origin/')[2])]}\n"
        self.run_fake.responses[("git", "-C", str(clone), "ls-remote")] = ls_remote
        self.run_fake.responses[("git", "-C", str(clone), "rev-list")] = ahead

    def push(self, branch, ahead=2, clone=None):
        """An earlier pass's branch on the clone's origin, ahead commits beyond base."""
        self.remotes[(str(clone or self.repo), branch)] = ahead

    def link(self, number, name, repo="acme/widgets", branch_repo=None):
        """A branch linked to the issue, as `gh issue develop` links one, in its own repo or branch_repo, and so on its
        origin, at base unless pushed already."""
        branch_repo = branch_repo or repo
        self.linked.setdefault((repo, number), []).append((branch_repo, name))
        if branch_repo in self.clones:
            self.remotes.setdefault((str(self.clones[branch_repo]), name), 0)

    def linked_branches(self, cmd):
        """GitHub's GraphQL answer for an issue's linked branches."""
        fields = dict(cmd[i + 1].split("=", 1) for i, arg in enumerate(cmd) if arg in ("-f", "-F"))
        links = self.linked.get((f"{fields['owner']}/{fields['name']}", int(fields["number"])), [])
        gone = lambda repo, name: repo in self.clones and (str(self.clones[repo]), name) not in self.remotes
        # A linked branch since deleted has no ref.
        nodes = [{"ref": None if gone(repo, name) else {"name": name, "repository": {"nameWithOwner": repo}}}
                 for repo, name in links]
        return json.dumps({"data": {"repository": {"issue": {"linkedBranches": {"nodes": nodes}}}}})

    def develop(self, cmd):
        """`gh issue develop`: a branch made and linked to the issue, named as GitHub names it unless --name is given."""
        option = lambda name: cmd[cmd.index(name) + 1] if name in cmd else None
        repo, number = option("--repo"), int(cmd[3])
        title = self.issues[self.key(repo, number)]["title"]
        name, branch_repo = option("--name") or f"{number}-{re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')}", option("--branch-repo") or repo
        if any((branch_repo, name) in links for links in self.linked.values()) and not (
                branch_repo in self.clones and (str(self.clones[branch_repo]), name) not in self.remotes):
            raise subprocess.CalledProcessError(1, cmd, stderr="Reference already exists")
        self.link(number, name, repo, branch_repo)  # on the remote, at base
        return f"github.com/{branch_repo}/tree/{name}\n"

    def queue_fails(self, *argv):
        out = io.StringIO()
        code = afk.main(list(argv), run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out, clock=lambda: self.now)
        self.assertEqual(code, 1, out.getvalue())
        return out.getvalue()


class EnqueueTest(QueueTestCase):
    def test_enqueue_from_a_session_bound_to_no_project_adds_the_ticket_to_the_queue(self):
        output = self.afk("enqueue", self.URL)

        self.assertIn("enqueued acme/widgets#12 at position 1", output)
        self.assertEqual(self.run_fake.find("tmux"), [])
        listing = self.afk("queue")
        self.assertIn("1. acme/widgets#12  Widget export", listing)
        self.assertIn("feature", listing)

    def test_a_ticket_and_its_repo_are_named_as_github_names_them_whatever_the_urls_casing(self):
        self.afk("enqueue", "https://github.com/ACME/Widgets/issues/12")

        self.assertIn("1. acme/widgets#12  Widget export  (acme/widgets: ", self.afk("queue"))

    def test_tickets_wait_in_the_order_they_were_enqueued(self):
        self.issue(13, "Widget import")
        self.issue(9, "Widget search")

        for number in (12, 13, 9):
            self.afk("enqueue", f"https://github.com/acme/widgets/issues/{number}")

        listing = self.afk("queue").splitlines()
        self.assertEqual([line.split("  ")[0] for line in listing],
                         ["1. acme/widgets#12", "2. acme/widgets#13", "3. acme/widgets#9"])

    def test_enqueueing_a_waiting_ticket_again_changes_nothing_and_says_where_it_waits(self):
        self.issue(13, "Widget import")
        self.afk("enqueue", self.URL)
        self.afk("enqueue", "https://github.com/acme/widgets/issues/13")

        output = self.afk("enqueue", "https://github.com/ACME/Widgets/issues/12", "--model", "opus")

        self.assertIn("acme/widgets#12 is already waiting, at position 1", output)
        self.assertIn("its launch choices are unchanged", output)
        listing = self.afk("queue").splitlines()
        self.assertEqual(len(listing), 2)
        self.assertNotIn("opus", listing[0])


class EnqueueChoicesTest(QueueTestCase):
    def setUp(self):
        super().setUp()
        self.repo_config('default_type = "feature"\n\n[task_types.feature]\nmodel = "sonnet"\neffort = "medium"\n\n'
                         '[task_types.bugfix]\nmodel = "haiku"\neffort = "low"\n')

    def test_the_tickets_type_line_picks_its_launch_choices_over_the_repos_default(self):
        self.issue(12, "Widget export", body="Type: bugfix\n")

        self.afk("enqueue", self.URL)

        self.assertIn("(acme/widgets: type bugfix, agent claude, model haiku, effort low)", self.afk("queue"))

    def test_flags_correct_the_launch_choices_and_are_kept_with_the_queued_ticket(self):
        self.afk("enqueue", self.URL, "--type", "bugfix", "--model", "opus")

        self.assertIn("(acme/widgets: type bugfix, agent claude, model opus, effort low)", self.afk("queue"))

    def test_malformed_options_fail_with_usage_and_queue_nothing(self):
        for argv in (["--model"], ["--colour", "blue"], ["--model", "opus", "--model", "haiku"]):
            self.assertIn("usage: afk enqueue", self.queue_fails("enqueue", self.URL, *argv))
        self.assertIn("usage: afk enqueue", self.queue_fails("enqueue", "https://example.com/issues/12"))
        self.assertIn("the queue is empty", self.afk("queue"))


class EnqueueRefusalTest(QueueTestCase):
    def refused(self, *argv):
        output = self.queue_fails("enqueue", self.URL, *argv)
        self.assertIn("the queue is empty", self.afk("queue"))
        return output

    def test_a_closed_ticket_is_refused(self):
        self.issue(12, "Widget export", state="closed")

        self.assertIn("acme/widgets#12 is closed", self.refused())

    def test_a_ticket_assigned_to_someone_else_is_refused_naming_them(self):
        self.issue(12, "Widget export", assignees=["me", "alice"])

        self.assertIn("acme/widgets#12 is assigned to alice", self.refused())

    def test_a_ticket_assigned_to_me_is_accepted(self):
        self.issue(12, "Widget export", assignees=["Me"])

        self.assertIn("enqueued acme/widgets#12", self.afk("enqueue", self.URL))

    def test_a_hitl_ticket_is_refused_by_its_type_line_or_a_flag(self):
        self.issue(12, "Widget export", body="Type: hitl\n")
        self.assertIn("hitl", self.refused())

        self.issue(12, "Widget export")
        self.assertIn("hitl", self.refused("--type", "hitl"))

    def test_a_hitl_ticket_is_refused_as_hitl_even_when_its_repo_has_no_clone(self):
        self.issue(12, "Widget export", body="Type: hitl\nRepo: acme/nowhere\n")

        self.assertIn("is a hitl ticket", self.refused())

    def test_a_pull_request_is_refused(self):
        self.issue(12, "Widget export")
        self.issues[12]["html_url"] = "https://github.com/acme/widgets/pull/12"

        self.assertIn(f"{self.URL} is a pull request, not an issue", self.refused())

    def test_an_unknown_task_type_is_refused_naming_the_defined_ones(self):
        self.assertIn("unknown task type 'chore'; docs/agents/afk.md defines: feature", self.refused("--type", "chore"))

    def test_an_unsupported_agent_is_refused(self):
        self.assertIn("agent 'codex'", self.refused("--agent", "codex"))


class EnqueueCloneTest(QueueTestCase):
    """The ticket's clone is searched for from the working directory, as `afk init` searches, then in [repos]."""

    def setUp(self):
        super().setUp()
        self.issue(12, "Widget export", body="Repo: acme/api\n")

    def api_clone(self, path):
        """A clone of acme/api whose repo config is told apart from widgets' by its default type."""
        self.clone(path, "git@github.com:acme/api.git")
        (path / "docs" / "agents").mkdir(parents=True)
        (path / "docs" / "agents" / "afk.md").write_text('+++\ndefault_type = "endpoint"\n\n[task_types.endpoint]\n+++\n')
        return path

    def register(self, repos):
        (self.state_home / "afk" / ".queue").mkdir(parents=True, exist_ok=True)
        (self.state_home / "afk" / ".queue" / "config.toml").write_text(afk.to_toml({"queue": {"max_workers": 1}, "repos": repos}))

    def test_a_clone_beside_the_working_checkout_is_found_by_its_remote(self):
        self.api_clone(self.tmp / "api-checkout")

        self.afk("enqueue", self.URL)

        self.assertIn("(acme/api: type endpoint", self.afk("queue"))

    def test_a_repo_with_no_clone_nearby_is_found_in_the_queues_repos_registry(self):
        elsewhere = self.api_clone(self.tmp / "far" / "away" / "api")
        self.register({"acme/web": str(self.tmp / "web"), "ACME/API": str(elsewhere)})

        self.afk("enqueue", self.URL)

        self.assertIn("(acme/api: type endpoint", self.afk("queue"))

    def test_outside_any_checkout_the_registry_still_finds_the_clone(self):
        def not_a_checkout(cmd):
            raise subprocess.CalledProcessError(128, cmd, stderr="fatal: not a git repository")

        self.run_fake.responses[("git", "rev-parse", "--show-toplevel")] = not_a_checkout
        self.register({"acme/api": str(self.api_clone(self.tmp / "far" / "api"))})
        nowhere = self.tmp / "nowhere" / "here"
        nowhere.mkdir(parents=True)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(nowhere)

        self.afk("enqueue", self.URL)

        self.assertIn("(acme/api: type endpoint", self.afk("queue"))

    def test_a_registered_path_may_start_from_home(self):
        self.api_clone(self.tmp / "far" / "api")
        self.register({"acme/api": "~/far/api"})

        self.afk("enqueue", self.URL)

        self.assertIn("(acme/api: type endpoint", self.afk("queue"))

    def test_a_registered_path_that_is_not_a_directory_is_named_in_the_refusal(self):
        self.register({"acme/api": str(self.tmp / "moved" / "api")})

        self.assertIn(f"acme/api = {self.tmp / 'moved' / 'api'} in [repos] is not a directory",
                      self.queue_fails("enqueue", self.URL))

    def test_a_malformed_queue_config_is_read_only_when_needed_and_named_when_it_is(self):
        (self.state_home / "afk" / ".queue").mkdir(parents=True)
        config = self.state_home / "afk" / ".queue" / "config.toml"
        config.write_text("[repos\n")

        self.assertIn(f"afk: {config} is not valid TOML", self.queue_fails("enqueue", self.URL))
        self.api_clone(self.tmp / "api-checkout")
        self.assertIn("enqueued acme/widgets#12", self.afk("enqueue", self.URL))

    def test_run_from_a_worktree_the_clone_is_its_main_checkout(self):
        worktree = self.tmp / "state" / "afk" / "widgets-2" / "worktrees" / "7"
        worktree.mkdir(parents=True)
        (worktree / ".git").write_text(f"gitdir: {self.repo}/.git/worktrees/7\n")
        self.run_fake.responses[("git", "rev-parse", "--show-toplevel")] = f"{worktree}\n"
        self.run_fake.responses[("git", "rev-parse", "--path-format=absolute")] = f"{self.repo}/.git/worktrees/7\n{self.repo}/.git\n"
        self.run_fake.responses[("git", "-C", str(worktree), "remote", "-v")] = "origin\tgit@github.com:acme/widgets.git (fetch)\n"

        self.afk("enqueue", self.URL, "--repo", "acme/widgets")

        [queued] = afk.read_json(self.state_home / "afk" / ".queue" / "queue.json")["tickets"]
        self.assertEqual(queued["clone"], str(self.repo))

    def test_the_repo_flag_picks_the_repo_over_the_tickets_repo_line(self):
        self.api_clone(self.tmp / "api-checkout")

        self.afk("enqueue", self.URL, "--repo", "acme/widgets")

        self.assertIn("(acme/widgets: type feature", self.afk("queue"))

    def test_a_repo_with_no_known_clone_is_refused_saying_where_to_put_one(self):
        output = self.queue_fails("enqueue", self.URL)

        self.assertIn("no clone of acme/api", output)
        self.assertIn(str(self.state_home / "afk" / ".queue" / "config.toml"), output)
        self.assertIn("the queue is empty", self.afk("queue"))

    def test_a_clone_without_an_afk_repo_config_is_refused_pointing_at_afk_setup(self):
        bare = self.clone(self.tmp / "api-checkout", "git@github.com:acme/api.git")

        output = self.queue_fails("enqueue", self.URL)

        self.assertIn(f"no afk repo config in {bare}", output)
        self.assertIn("/afk-setup", output)
        self.assertIn("the queue is empty", self.afk("queue"))


class DequeueTest(QueueTestCase):
    def setUp(self):
        super().setUp()
        for number in (12, 13, 14):
            self.issue(number, f"Widget {number}")
            self.afk("enqueue", f"https://github.com/acme/widgets/issues/{number}")

    def ids(self):
        return [line.split("  ")[0].split(". ")[1] for line in self.afk("queue").splitlines()]

    def test_dequeue_removes_a_waiting_ticket_keeping_the_others_in_order(self):
        output = self.afk("dequeue", "acme/widgets#13")

        self.assertIn("removed acme/widgets#13 from the queue", output)
        self.assertEqual(self.ids(), ["acme/widgets#12", "acme/widgets#14"])

    def test_a_ticket_is_named_by_its_url_or_its_id_in_any_casing(self):
        self.afk("dequeue", "https://github.com/acme/widgets/issues/12")
        self.afk("dequeue", "ACME/Widgets#14")

        self.assertEqual(self.ids(), ["acme/widgets#13"])

    def test_a_dequeued_ticket_can_be_enqueued_again_at_the_end(self):
        self.afk("dequeue", "acme/widgets#12")

        self.assertIn("at position 3", self.afk("enqueue", self.URL))
        self.assertEqual(self.ids(), ["acme/widgets#13", "acme/widgets#14", "acme/widgets#12"])

    def test_a_ticket_not_in_the_queue_is_refused(self):
        self.assertIn("acme/widgets#99 is not in the queue", self.queue_fails("dequeue", "acme/widgets#99"))
        self.assertIn("usage: afk dequeue", self.queue_fails("dequeue"))
        self.assertEqual(len(self.ids()), 3)


class QueuePauseTest(QueueTestCase):
    def test_pause_and_resume_set_the_queues_paused_flag_which_the_listing_shows(self):
        self.afk("enqueue", self.URL)

        self.assertIn("queue paused", self.afk("queue", "pause"))
        listing = self.afk("queue")
        self.assertIn("paused", listing.splitlines()[0])
        self.assertIn("1. acme/widgets#12", listing)

        self.assertIn("queue resumed", self.afk("queue", "resume"))
        self.assertNotIn("paused", self.afk("queue"))

    def test_an_empty_queue_can_be_paused(self):
        self.afk("queue", "pause")

        self.assertIn("paused", self.afk("queue"))
        self.assertIn("the queue is empty", self.afk("queue"))

    def test_an_unknown_subcommand_fails_with_usage(self):
        self.assertIn("usage: afk queue", self.queue_fails("queue", "flush"))


class QueueTickTestCase(QueueTestCase):
    """`afk queue tick`: the queue's watcher picking tickets up and driving their workers."""

    def setUp(self):
        super().setUp()
        self.run_fake.responses[("tmux", "split-window")] = "%6\n"

    def enqueue(self, *numbers):
        for number in numbers:
            if self.key("acme/widgets", number) not in self.issues:
                self.issue(number, f"Widget {number}")
            self.afk("enqueue", f"https://github.com/acme/widgets/issues/{number}")

    def worktree_adds(self):
        return [c for c in self.run_fake.find("git", "-C") if c[3:5] == ["worktree", "add"]]

    def desktop_notifications(self):
        return [" ".join(c[1:]) for c in self.run_fake.find("notify-send")]


class QueueTickTest(QueueTickTestCase):
    def test_a_tick_starts_the_oldest_waiting_ticket_claimed_on_its_own_branch_in_the_queues_session(self):
        self.enqueue(12, 13)

        self.afk("queue", "tick")

        self.assertEqual(self.run_fake.find("gh", "issue", "edit"),
                         [["gh", "issue", "edit", "12", "--repo", "acme/widgets", "--add-assignee", "@me"]])
        [add] = self.worktree_adds()
        self.assertEqual(add[2], str(self.repo))
        self.assertEqual(add[5:7], ["-b", "12-widget-export"])
        self.assertEqual(add[-1], "origin/main")
        [window] = self.run_fake.find("tmux", "new-window")
        self.assertEqual(window[window.index("-t") + 1], "afk-queue:")
        [send] = self.run_fake.find("tmux", "send-keys")
        launch = shlex.split(send[4])
        self.assertIn("AFK_SLOT=8", launch)
        self.assertTrue(launch[-1].startswith("AFK phase: implement"), launch[-1])
        self.assertIn(self.URL, launch[-1])
        listing = self.afk("queue").splitlines()
        self.assertTrue(listing[0].startswith("running  acme/widgets#12  Widget export  (implement, working)"), listing)
        self.assertTrue(listing[1].startswith("1. acme/widgets#13"), listing)

    def test_a_paused_queue_starts_nothing_until_it_is_resumed(self):
        self.enqueue(12)
        self.afk("queue", "pause")

        self.afk("queue", "tick")

        self.assertEqual(self.worktree_adds(), [])
        self.afk("queue", "resume")
        self.afk("queue", "tick")
        self.assertEqual(len(self.worktree_adds()), 1)

    def test_a_ticket_waits_while_every_queue_worker_slot_is_taken(self):
        self.enqueue(12, 13)
        self.afk("queue", "tick")

        self.afk("queue", "tick")

        self.assertEqual(len(self.worktree_adds()), 1)

    def test_max_workers_lets_more_run_each_on_the_next_slot_down(self):
        self.enqueue(12, 13, 14)
        (self.state_home / "afk" / ".queue" / "config.toml").write_text("[queue]\nmax_workers = 2\n")

        self.afk("queue", "tick")

        self.assertEqual([add[6] for add in self.worktree_adds()], ["12-widget-export", "13-widget-13"])
        slots = [next(arg for arg in shlex.split(send[4]) if arg.startswith("AFK_SLOT="))
                 for send in self.run_fake.find("tmux", "send-keys")]
        self.assertEqual(slots, ["AFK_SLOT=8", "AFK_SLOT=7"])

    def test_max_workers_beyond_the_queues_eight_slots_runs_eight_and_the_rest_wait(self):
        self.enqueue(*range(12, 21))
        (self.state_home / "afk" / ".queue" / "config.toml").write_text("[queue]\nmax_workers = 20\n")

        self.afk("queue", "tick")
        self.afk("queue", "tick")

        self.assertEqual(len(self.worktree_adds()), 8)
        self.assertIn("1. acme/widgets#20", self.afk("queue"))


class QueuePickUpTest(QueueTickTestCase):
    """The checks a waiting ticket passes, re-read from its tracker, before a worker starts on it."""

    def test_a_ticket_closed_while_it_waited_is_removed_with_a_notification_and_the_next_one_started(self):
        self.enqueue(12, 13)
        self.issue(12, "Widget export", state="closed")

        output = self.afk("queue", "tick")

        self.assertEqual([add[6] for add in self.worktree_adds()], ["13-widget-13"])
        self.assertEqual(self.run_fake.find("gh", "issue", "edit", "12"), [])
        [notification] = self.desktop_notifications()
        self.assertIn("acme/widgets#12 removed from the queue: it was closed", notification)
        self.assertNotIn("\a", output)
        listing = self.afk("queue")
        self.assertIn("removed  acme/widgets#12  Widget export  it was closed", listing)
        self.assertNotIn("1. acme/widgets#12", listing)

    def test_a_ticket_assigned_to_someone_else_while_it_waited_is_removed_naming_them(self):
        self.enqueue(12)
        self.issue(12, "Widget export", assignees=["octocat"])

        self.afk("queue", "tick")

        self.assertEqual(self.worktree_adds(), [])
        self.assertIn("it was assigned to octocat", self.desktop_notifications()[0])
        self.assertIn("removed  acme/widgets#12  Widget export  it was assigned to octocat", self.afk("queue"))

    def test_a_blocked_ticket_is_skipped_keeping_its_place_and_started_once_unblocked(self):
        self.issue(3, "Widget schema")
        self.native_blockers[12] = [3]
        self.enqueue(12, 13)

        output = self.afk("queue", "tick")

        self.assertEqual([add[6] for add in self.worktree_adds()], ["13-widget-13"])
        self.assertEqual(self.desktop_notifications(), [])
        self.assertNotIn("\a", output)
        self.assertIn("1. acme/widgets#12  Widget export  (acme/widgets: type feature, agent claude, model sonnet, "
                      "effort medium)  blocked by #3", self.afk("queue"))

        (self.state_home / "afk" / ".queue" / "config.toml").write_text("[queue]\nmax_workers = 2\n")
        self.afk("queue", "tick")
        self.assertEqual(len(self.worktree_adds()), 1)  # blocked a moment ago, so not asked again yet

        self.issue(3, "Widget schema", state="closed")
        self.now += 60
        self.afk("queue", "tick")
        self.assertEqual([add[6] for add in self.worktree_adds()], ["13-widget-13", "12-widget-export"])

    def test_a_ticket_numbered_as_a_running_one_in_another_repo_waits_for_it_to_end(self):
        gadgets = self.clone(self.tmp / "gadgets", "git@github.com:acme/gadgets.git")
        (gadgets / "docs" / "agents").mkdir(parents=True)
        (gadgets / "docs" / "agents" / "afk.md").write_text((self.repo / "docs" / "agents" / "afk.md").read_text())
        self.issue(12, "Gadget export", repo="acme/gadgets")
        self.enqueue(12)
        self.afk("enqueue", "https://github.com/acme/gadgets/issues/12")
        self.enqueue(13)
        (self.state_home / "afk" / ".queue" / "config.toml").write_text("[queue]\nmax_workers = 2\n")

        self.afk("queue", "tick")

        self.assertEqual([(add[2], add[6]) for add in self.worktree_adds()],
                         [(str(self.repo), "12-widget-export"), (str(self.repo), "13-widget-13")])
        self.assertIn("1. acme/gadgets#12", self.afk("queue"))

    def test_a_ticket_gone_from_github_is_removed_with_a_notification_and_the_next_one_started(self):
        self.enqueue(12, 13)
        del self.issues[12]
        found = self.gh_api

        def gh_api(cmd):
            if any(arg.lower() == "repos/acme/widgets/issues/12" for arg in cmd):
                raise subprocess.CalledProcessError(1, cmd, stderr="gh: Not Found (HTTP 404)")
            return found(cmd)

        self.run_fake.responses[("gh", "api")] = gh_api

        self.afk("queue", "tick")

        self.assertEqual([add[6] for add in self.worktree_adds()], ["13-widget-13"])
        self.assertIn("acme/widgets#12 removed from the queue: it was not found", self.desktop_notifications()[0])
        self.assertIn("removed  acme/widgets#12  Widget export  it was not found", self.afk("queue"))

    def test_a_ticket_that_cannot_be_checked_keeps_its_place_and_the_next_one_starts(self):
        self.issue(12, "Widget export", body="## Blocked by\n\n#99\n")  # an issue gone, or out of reach
        self.enqueue(12, 13)
        found = self.gh_api

        def gh_api(cmd):
            if any(arg.lower() == "repos/acme/widgets/issues/99" for arg in cmd):
                raise subprocess.CalledProcessError(1, cmd, stderr="gh: Not Found (HTTP 404)")
            return found(cmd)

        self.run_fake.responses[("gh", "api")] = gh_api

        self.afk("queue", "tick")

        self.assertEqual([add[6] for add in self.worktree_adds()], ["13-widget-13"])
        self.assertIn("1. acme/widgets#12", self.afk("queue"))
        self.assertIn("couldn't check it: `gh api repos/acme/widgets/issues/99` failed", self.afk("queue"))

    def test_a_start_the_watcher_was_killed_in_is_undone_and_tried_again(self):
        self.enqueue(12)
        # What a watcher killed mid-bootstrap leaves, as no rollback runs: the ticket running, a slot without a worker.
        queue = self.state_home / "afk" / ".queue"
        state = json.loads((queue / "queue.json").read_text())
        state["tickets"][0].update(state="running", worker="12", branch="12-widget-export")
        (queue / "queue.json").write_text(json.dumps(state))
        (queue / "workers" / "12").mkdir(parents=True)
        (queue / "workers" / "12" / "slot").write_text("8\n")
        (queue / "worktrees" / "12").mkdir(parents=True)  # added, with its branch, before the bootstrap

        self.afk("queue", "tick")

        worktree = str(queue / "worktrees" / "12")
        self.assertIn(["git", "-C", str(self.repo), "worktree", "remove", "--force", worktree], self.run_fake.calls)
        self.assertIn(["git", "-C", str(self.repo), "branch", "-D", "12-widget-export"], self.run_fake.calls)
        self.assertIn("running  acme/widgets#12  Widget export  (implement, working)", self.afk("queue"))
        self.assertIn("AFK_SLOT=8", self.run_fake.find("tmux", "send-keys")[-1][4])

    def test_a_start_killed_before_its_worktree_was_added_leaves_a_branch_of_that_name_alone(self):
        self.enqueue(12)
        queue = self.state_home / "afk" / ".queue"
        state = json.loads((queue / "queue.json").read_text())
        state["tickets"][0].update(state="running", worker="12", branch="12-widget-export")
        (queue / "queue.json").write_text(json.dumps(state))

        self.afk("queue", "tick")

        # Not afk's to delete: the worktree that would have made it was never added, so it may be the engineer's.
        self.assertNotIn(["git", "-C", str(self.repo), "branch", "-D", "12-widget-export"], self.run_fake.calls)
        self.assertIn("running  acme/widgets#12", self.afk("queue"))

    def test_a_ticket_whose_worker_fails_to_start_keeps_its_place_says_why_and_is_retried_a_poll_later(self):
        def split_fails(cmd):
            raise subprocess.CalledProcessError(1, cmd, stderr="no space for new pane")

        self.run_fake.responses[("tmux", "split-window")] = split_fails
        self.enqueue(12, 13)

        output = self.afk("queue", "tick")

        self.assertEqual([add[6] for add in self.worktree_adds()], ["12-widget-export", "13-widget-13"])  # both tried
        self.assertIn("1. acme/widgets#12  Widget export", self.afk("queue"))
        self.assertIn("failed to start: `tmux split-window", self.afk("queue"))
        self.assertIn("no space for new pane", self.afk("queue"))
        self.assertEqual(len(self.desktop_notifications()), 2)
        self.assertIn("acme/widgets#12 failed to start", self.desktop_notifications()[0])
        self.assertIn("\a", output)

        self.afk("queue", "tick")
        self.assertEqual(len(self.worktree_adds()), 2)  # not tried again until a poll later

        self.now += 60
        self.afk("queue", "tick")
        self.assertEqual(len(self.worktree_adds()), 4)
        self.assertEqual(len(self.desktop_notifications()), 2)  # the same failure again notifies nothing new


class QueueWorkerTestCase(QueueTickTestCase):
    """The queue with acme/widgets#12's worker started, and helpers to act as it."""

    def setUp(self):
        super().setUp()
        self.before_pick_up()
        self.enqueue(12)
        self.afk("queue", "tick")
        self.calls_before = len(self.run_fake.calls)

    def before_pick_up(self):
        """Script what the tick that starts the worker finds."""

    def worker_env(self):
        return {"AFK_QUEUE": "1", "AFK_TICKET": "12"}

    def finish_phase(self, message="Done"):
        self.afk("report", "done", message, env=self.worker_env())
        payload = {"session_id": "s", "transcript_path": str(self.tmp / "12.jsonl"), "hook_event_name": "Stop"}
        self.afk("hook", "stop", stdin=json.dumps(payload), env=self.worker_env())
        return self.afk("queue", "tick")

    def status(self):
        return json.loads((self.state_home / "afk" / ".queue" / "workers" / "12" / "status.json").read_text())

    def prompts_sent(self, pane="%5"):
        return WorkerTestCase.prompts_sent(self, pane)

    def denied(self, command):
        """Whether the worker's settings deny the shell command."""
        settings = json.loads((self.state_home / "afk" / ".queue" / "workers" / "12" / "settings.json").read_text())
        return any(fnmatch.fnmatchcase(f"Bash({command})", rule) for rule in settings["permissions"]["deny"])

    def about_to(self, tool, env=None, **tool_input):
        """What the worker's PreToolUse hook says to the tool call, or None if it lets it be."""
        worktree = self.state_home / "afk" / ".queue" / "worktrees" / "12"
        payload = {"session_id": "s", "transcript_path": str(self.tmp / "12.jsonl"), "hook_event_name": "PreToolUse",
                   "cwd": str(worktree), "tool_name": tool, "tool_input": tool_input}
        output = self.afk("hook", "activity", stdin=json.dumps(payload), env=env or self.worker_env())
        return json.loads(output)["hookSpecificOutput"] if output.strip() else None


class QueuePhaseTest(QueueWorkerTestCase):
    def test_each_done_phase_advances_to_the_next_critiquing_and_then_opening_a_draft(self):
        for _ in range(4):
            self.finish_phase()

        self.assertEqual((self.status()["phase"], self.status()["state"]), ("draft", "working"))
        self.assertEqual(
            [p.splitlines()[0].split(" — ")[0] for p in self.prompts_sent()],
            ["AFK phase: verify", "AFK phase: prepr", "AFK phase: critique", "AFK phase: draft"],
        )
        self.assertIn("running  acme/widgets#12  Widget export  (draft, working)", self.afk("queue"))

    def test_critique_runs_the_repos_reviewers_over_the_diff_against_its_base_as_prepr_merged_it(self):
        self.repo_config('default_type = "feature"\nreviewers = ["coderabbit review --base {{target}} --plain"]\n\n'
                         '[task_types.feature]\nmodel = "sonnet"\n')
        for _ in range(3):
            self.finish_phase()

        critique = self.prompts_sent()[-1]
        self.assertTrue(critique.startswith("AFK phase: critique"), critique)
        self.assertIn("`coderabbit review --base origin/main --plain`", critique)
        self.assertIn("git diff origin/main...HEAD", critique)
        self.assertNotIn("{{", critique)

    def test_the_draft_prompt_names_the_ticket_the_base_and_the_ci_rounds(self):
        (self.state_home / "afk" / ".queue" / "config.toml").write_text("[queue.limits]\nci_rounds = 5\n")
        for _ in range(4):
            self.finish_phase()

        draft = self.prompts_sent()[-1]
        self.assertIn(self.URL, draft)
        self.assertIn("--base main", draft)
        self.assertIn("5 rounds", draft)
        self.assertNotIn("{{", draft)

    def test_the_draft_prompt_allows_two_ci_rounds_by_default(self):
        for _ in range(4):
            self.finish_phase()

        self.assertIn("2 rounds", self.prompts_sent()[-1])

    def test_every_phase_prompt_has_the_worker_keep_its_questions_for_the_draft_instead_of_asking(self):
        for _ in range(4):
            self.finish_phase()

        [send] = self.run_fake.find("tmux", "send-keys", "-t", "%5")[:1]
        for prompt in [shlex.split(send[4])[-1], *self.prompts_sent()]:
            self.assertIn("## Questions for the engineer", prompt)


class QueueDraftTest(QueueWorkerTestCase):
    """A queue worker that reports done in the draft phase, its draft PR open."""

    DRAFT = "https://github.com/acme/widgets/pull/7"

    def setUp(self):
        super().setUp()
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%6")] = "bash\n"  # its shell pane is idle
        for _ in range(4):
            self.finish_phase()
        self.run_fake.responses[("gh", "pr", "view")] = pr_view("OPEN", url=self.DRAFT)
        self.now += 60

    def test_the_ticket_gets_a_comment_linking_its_draft_and_stays_open_and_assigned(self):
        self.finish_phase("Draft open, CI green")

        [comment] = self.run_fake.find("gh", "issue", "comment")
        self.assertEqual(comment[3:6], ["12", "--repo", "acme/widgets"])
        self.assertIn(self.DRAFT, comment[comment.index("--body") + 1])
        self.assertEqual(self.run_fake.find("gh", "issue", "close"), [])
        self.assertEqual([c for c in self.run_fake.find("gh", "issue", "edit") if "--remove-assignee" in c], [])

    def test_its_window_worktree_and_local_branch_are_cleaned_up_with_a_notification_but_no_bell(self):
        output = self.finish_phase("Draft open, CI green")

        worktree = str(self.state_home / "afk" / ".queue" / "worktrees" / "12")
        self.assertEqual(self.run_fake.find("tmux", "kill-window"), [["tmux", "kill-window", "-t", "%5"]])
        self.assertEqual(self.run_fake.find("git", "-C", str(self.repo), "worktree", "remove"),
                         [["git", "-C", str(self.repo), "worktree", "remove", "--force", worktree]])
        self.assertEqual(self.run_fake.find("git", "-C", str(self.repo), "branch", "-D"),
                         [["git", "-C", str(self.repo), "branch", "-D", "12-widget-export"]])
        self.assertFalse((self.state_home / "afk" / ".queue" / "workers" / "12").exists())
        [notification] = self.desktop_notifications()
        self.assertIn(f"acme/widgets#12 draft PR opened: {self.DRAFT}", notification)
        self.assertNotIn("\a", output)

    def test_a_worker_started_before_its_ticket_named_its_branch_is_ended_on_the_branch_afk_named(self):
        path = self.state_home / "afk" / ".queue" / "workers" / "12" / "status.json"
        status = json.loads(path.read_text())
        del status["branch"]
        path.write_text(json.dumps(status))

        self.finish_phase("Draft open, CI green")

        self.assertEqual(self.run_fake.find("gh", "pr", "view")[-1][3], "afk/queue-12")
        self.assertIn(["git", "-C", str(self.repo), "branch", "-D", "afk/queue-12"], self.run_fake.calls)

    def test_afk_queue_shows_the_draft_among_recent_outcomes(self):
        self.finish_phase("Draft open, CI green")

        listing = self.afk("queue")
        self.assertIn(f"drafted  acme/widgets#12  Widget export  {self.DRAFT}", listing)
        self.assertNotIn("running", listing)

    def test_enqueued_again_once_drafted_it_is_accepted_and_resumes_on_its_pushed_branch(self):
        self.finish_phase("Draft open, CI green")
        self.run_fake.responses[("git", "-C", str(self.repo), "ls-remote")] = "3f2a9c1\trefs/heads/12-widget-export\n"
        self.run_fake.responses[("gh", "pr", "list")] = json.dumps([{"url": self.DRAFT}])
        self.run_fake.responses[("git", "-C", str(self.repo), "rev-list")] = "4\n"

        self.assertIn("enqueued acme/widgets#12 at position 1", self.afk("enqueue", self.URL))
        self.afk("queue", "tick")

        self.assertEqual([add[-1] for add in self.worktree_adds()], ["origin/main", "origin/12-widget-export"])
        resume = shlex.split(self.run_fake.find("tmux", "send-keys")[-1][4])[-1]
        self.assertTrue(resume.startswith("AFK phase: resume"), resume)
        self.assertIn(self.DRAFT, resume)

    def test_enqueued_again_once_its_branch_is_deleted_it_starts_afresh_from_base(self):
        self.finish_phase("Draft open, CI green")
        del self.remotes[(str(self.repo), "12-widget-export")]

        self.afk("enqueue", self.URL)
        self.afk("queue", "tick")

        # GitHub drops the deleted branch's link, so the ticket gets a new one, from base.
        self.assertEqual(len(self.run_fake.find("gh", "issue", "develop")), 2)
        self.assertEqual([add[-1] for add in self.worktree_adds()], ["origin/main", "origin/main"])
        implement = shlex.split(self.run_fake.find("tmux", "send-keys")[-1][4])[-1]
        self.assertTrue(implement.startswith("AFK phase: implement"), implement)

    def test_the_next_waiting_ticket_starts_in_the_same_tick_on_the_freed_slot(self):
        self.enqueue(13)
        self.assertEqual(len(self.worktree_adds()), 1)

        self.finish_phase("Draft open, CI green")

        self.assertEqual([add[6] for add in self.worktree_adds()], ["12-widget-export", "13-widget-13"])
        launch = shlex.split(self.run_fake.find("tmux", "send-keys")[-1][4])
        self.assertIn("AFK_SLOT=8", launch)

    def test_done_without_a_pr_for_its_branch_needs_attention_instead(self):
        self.run_fake.responses[("gh", "pr", "view")] = no_pr

        output = self.finish_phase("Draft open")

        self.assertEqual((self.status()["phase"], self.status()["state"]), ("draft", "attention"))
        self.assertIn("12 needs attention: no PR found for its branch", self.desktop_notifications()[-1])
        self.assertIn("\a", output)
        self.assertEqual(self.run_fake.find("tmux", "kill-window"), [])

    def test_unpushed_commits_leave_it_cleanup_pending_and_the_queue_waits(self):
        worktree = str(self.state_home / "afk" / ".queue" / "worktrees" / "12")
        self.run_fake.responses[("git", "-C", worktree, "log")] = "3f2a9c1 Fix the export\n"
        self.enqueue(13)

        self.finish_phase("Draft open, CI green")

        self.assertEqual(self.status()["state"], "cleanup-pending")
        self.assertIn("unpushed commits", self.desktop_notifications()[-1])
        self.assertEqual(len(self.worktree_adds()), 1)
        self.assertEqual(self.run_fake.find("gh", "issue", "comment"), [])

        self.run_fake.responses[("git", "-C", worktree, "log")] = ""
        self.afk("queue", "tick")
        self.assertEqual(len(self.run_fake.find("gh", "issue", "comment")), 1)
        self.assertEqual(len(self.worktree_adds()), 2)

    def test_a_ticket_the_engineer_closes_mid_draft_ends_without_a_comment_and_the_next_one_starts(self):
        self.enqueue(13)
        self.issue(12, "Widget export", state="closed")

        self.afk("queue", "tick")

        self.assertFalse((self.state_home / "afk" / ".queue" / "workers" / "12").exists())
        self.assertEqual(self.run_fake.find("gh", "issue", "comment"), [])
        self.assertIn("ended  acme/widgets#12  Widget export  its ticket was closed", self.afk("queue"))
        self.assertEqual(len(self.worktree_adds()), 2)
        [notification] = self.desktop_notifications()
        self.assertIn("acme/widgets#12 ended: its ticket was closed", notification)
        self.assertNotIn("/afk next", notification)

    def pr_ends_mid_draft(self, state):
        self.enqueue(13)
        self.run_fake.responses[("gh", "pr", "view")] = pr_view(state, url=self.DRAFT)

        self.afk("queue", "tick")

        self.assertFalse((self.state_home / "afk" / ".queue" / "workers" / "12").exists())
        self.assertEqual([add[6] for add in self.worktree_adds()], ["12-widget-export", "13-widget-13"])
        return self.afk("queue")

    def test_a_pr_the_engineer_merges_mid_draft_ends_the_ticket_and_the_next_one_starts(self):
        self.assertIn("ended  acme/widgets#12  Widget export  its PR was merged", self.pr_ends_mid_draft("MERGED"))

    def test_a_pr_the_engineer_closes_mid_draft_ends_the_ticket_releasing_it_and_the_next_one_starts(self):
        self.assertIn("ended  acme/widgets#12  Widget export  its PR was closed", self.pr_ends_mid_draft("CLOSED"))
        self.assertIn(["gh", "issue", "edit", "12", "--repo", "acme/widgets", "--remove-assignee", "@me"],
                      self.run_fake.find("gh", "issue", "edit"))

    def test_a_pr_gone_by_the_time_the_ticket_ends_is_not_reported_as_its_draft(self):
        views = iter([pr_view("OPEN", url=self.DRAFT)])
        self.run_fake.responses[("gh", "pr", "view")] = lambda cmd: next(views, None) or no_pr(cmd)

        self.finish_phase("Draft open")

        self.assertEqual(self.run_fake.find("gh", "issue", "comment"), [])
        self.assertIn("ended  acme/widgets#12  Widget export  its PR was closed", self.afk("queue"))

    def test_a_running_worker_keeps_its_clone_when_a_ticket_of_its_repo_waits_in_another(self):
        other = self.clone(self.tmp / "widgets-2", "git@github.com:acme/widgets.git")
        (other / "docs" / "agents").mkdir(parents=True)
        (other / "docs" / "agents" / "afk.md").write_text((self.repo / "docs" / "agents" / "afk.md").read_text())
        self.run_fake.responses[("git", "rev-parse", "--show-toplevel")] = f"{other}\n"
        self.enqueue(13)

        self.finish_phase("Draft open")

        worktree = str(self.state_home / "afk" / ".queue" / "worktrees" / "12")
        self.assertEqual(self.run_fake.find("git", "-C", str(self.repo), "worktree", "remove"),
                         [["git", "-C", str(self.repo), "worktree", "remove", "--force", worktree]])
        self.assertEqual(self.worktree_adds()[-1][2], str(other))

    def test_a_ticket_deleted_while_drafting_still_ends_as_drafted_without_a_comment(self):
        self.enqueue(13)
        found = self.gh_api

        def gh_api(cmd):
            if any(arg.lower() == "repos/acme/widgets/issues/12" for arg in cmd):
                raise subprocess.CalledProcessError(1, cmd, stderr="gh: Not Found (HTTP 404)")
            return found(cmd)

        self.run_fake.responses[("gh", "api")] = gh_api

        self.finish_phase("Draft open")

        self.assertEqual(self.run_fake.find("gh", "issue", "comment"), [])
        self.assertIn(f"drafted  acme/widgets#12  Widget export  {self.DRAFT}", self.afk("queue"))
        self.assertEqual(self.worktree_adds()[-1][6], "13-widget-13")

    def test_a_cleanup_cut_short_once_the_ticket_ended_finishes_on_the_next_tick(self):
        self.enqueue(13)
        interrupted = []

        def kill_window(cmd):
            if not interrupted:
                interrupted.append(cmd)
                raise KeyboardInterrupt  # the watcher stopped mid-cleanup

        self.run_fake.responses[("tmux", "kill-window")] = kill_window
        self.afk("report", "done", "Draft open", env=self.worker_env())
        self.afk("hook", "stop", stdin=json.dumps({"session_id": "s", "hook_event_name": "Stop"}), env=self.worker_env())
        with self.assertRaises(KeyboardInterrupt):
            self.afk("queue", "tick")

        self.afk("queue", "tick")

        self.assertFalse((self.state_home / "afk" / ".queue" / "workers" / "12").exists())
        self.assertEqual(len(self.run_fake.find("gh", "issue", "comment")), 1)
        self.assertEqual(len(self.desktop_notifications()), 1)
        self.assertEqual([add[6] for add in self.worktree_adds()], ["12-widget-export", "13-widget-13"])

    def test_a_comment_that_fails_is_posted_on_the_next_tick_before_anything_is_cleaned_up(self):
        def comment_fails(cmd):
            raise subprocess.CalledProcessError(1, cmd, stderr="HTTP 502")

        self.run_fake.responses[("gh", "issue", "comment")] = comment_fails
        self.afk("report", "done", "Draft open", env=self.worker_env())
        self.afk("hook", "stop", stdin=json.dumps({"session_id": "s", "hook_event_name": "Stop"}), env=self.worker_env())
        self.queue_fails("queue", "tick")
        self.assertEqual(self.run_fake.find("tmux", "kill-window"), [])
        del self.run_fake.responses[("gh", "issue", "comment")]

        self.afk("queue", "tick")

        self.assertEqual(len(self.run_fake.find("gh", "issue", "comment")), 2)
        self.assertFalse((self.state_home / "afk" / ".queue" / "workers" / "12").exists())
        [notification] = self.desktop_notifications()
        self.assertIn("draft PR opened", notification)

    def test_a_pr_found_after_done_needed_attention_for_lack_of_one_ends_the_ticket(self):
        self.run_fake.responses[("gh", "pr", "view")] = no_pr
        self.finish_phase("Draft open")
        self.run_fake.responses[("gh", "pr", "view")] = pr_view("OPEN", url=self.DRAFT)
        self.now += 60

        self.afk("queue", "tick")

        self.assertFalse((self.state_home / "afk" / ".queue" / "workers" / "12").exists())
        self.assertIn(f"drafted  acme/widgets#12  Widget export  {self.DRAFT}", self.afk("queue"))


class QueueWrapUpTest(QueueWorkerTestCase):
    """A queue worker over a limit: told once to wrap up, and abandoned if it hasn't within the grace period."""

    def limits(self, **limits):
        (self.state_home / "afk" / ".queue" / "config.toml").write_text(
            "[queue.limits]\n" + "".join(f"{k} = {v}\n" for k, v in limits.items()))

    def interrupts(self):
        return [c for c in self.run_fake.calls[self.calls_before:] if c[:2] == ["tmux", "send-keys"] and c[-1] == "Escape"]

    def test_a_worker_over_its_phase_limit_is_interrupted_and_told_to_wrap_up_instead_of_going_stuck(self):
        self.limits(phase_minutes=5)
        self.now += 6 * 60

        self.afk("queue", "tick")

        self.assertNotEqual(self.status()["state"], "stuck")
        self.assertEqual(len(self.interrupts()), 1)
        [wrap_up] = self.prompts_sent()
        self.assertTrue(wrap_up.startswith("AFK phase: wrap-up"), wrap_up)
        self.assertIn("over 5m in implement", wrap_up)
        self.assertIn(self.URL, wrap_up)
        self.assertNotIn("{{", wrap_up)

    def test_a_worker_over_a_token_cap_is_interrupted_and_told_to_wrap_up_instead_of_going_stuck(self):
        self.limits(phase_tokens=25_000)
        model_response(self.tmp / "12.jsonl", 30_000, self.now)
        payload = {"session_id": "s", "transcript_path": str(self.tmp / "12.jsonl"), "hook_event_name": "PreToolUse"}
        self.afk("hook", "activity", stdin=json.dumps(payload), env=self.worker_env())

        self.afk("queue", "tick")

        self.assertNotEqual(self.status()["state"], "stuck")
        self.assertEqual(len(self.interrupts()), 1)
        [wrap_up] = self.prompts_sent()
        self.assertIn("over 25,000 tokens in implement", wrap_up)

    def test_a_queue_over_its_daily_token_cap_starts_nothing_new_until_its_usage_falls_back_under(self):
        (self.state_home / "afk" / ".queue" / "config.toml").write_text(
            "[queue]\nmax_workers = 2\n\n[queue.limits]\ndaily_tokens = 25_000\n")
        model_response(self.tmp / "12.jsonl", 30_000, self.now)
        payload = {"session_id": "s", "transcript_path": str(self.tmp / "12.jsonl"), "hook_event_name": "PreToolUse"}
        self.afk("hook", "activity", stdin=json.dumps(payload), env=self.worker_env())
        self.enqueue(13)

        self.afk("queue", "tick")
        self.afk("queue", "tick")

        self.assertIn("workers over 25,000 tokens in the last 24h", self.prompts_sent()[0])  # 12 wraps up
        started = lambda: [add[6] for add in self.worktree_adds()[1:]]  # after 12's
        self.assertEqual(started(), [])
        listing = self.afk("queue")
        self.assertIn("over 25,000 tokens in the last 24h: nothing new starts until it falls back under daily_tokens",
                      listing.splitlines()[0])
        self.assertIn("1. acme/widgets#13", listing)  # still waiting
        held = [n for n in self.desktop_notifications() if "nothing new starts" in n]
        self.assertEqual(held, ["afk: queue over 25,000 tokens in the last 24h: nothing new starts until it falls back "
                                "under daily_tokens"])

        self.now += 25 * 3600
        self.afk("queue", "tick")

        self.assertEqual(started(), ["13-widget-13"])

    def test_a_worker_working_but_quiet_past_its_idle_limit_is_interrupted_and_told_to_wrap_up(self):
        self.limits(idle_minutes=10)
        self.now += 11 * 60

        self.afk("queue", "tick")

        self.assertEqual(len(self.interrupts()), 1)
        [wrap_up] = self.prompts_sent()
        self.assertIn("idle for over 10m", wrap_up)

    def test_a_worker_that_stopped_without_reporting_and_stayed_quiet_is_told_to_wrap_up_without_an_interrupt(self):
        self.limits(idle_minutes=10)
        self.afk("hook", "stop", stdin=json.dumps({"session_id": "s", "hook_event_name": "Stop"}), env=self.worker_env())
        self.now += 11 * 60

        self.afk("queue", "tick")

        self.assertEqual(self.interrupts(), [])
        [wrap_up] = self.prompts_sent()
        self.assertTrue(wrap_up.startswith("AFK phase: wrap-up"), wrap_up)
        self.assertNotEqual(self.status()["state"], "attention")
        self.assertEqual(self.desktop_notifications(), [])

    def test_a_worker_still_fixing_after_its_critique_rounds_is_told_to_wrap_up(self):
        self.limits(critique_rounds=2)
        for _ in range(3):
            self.finish_phase()

        self.finish_phase("round: fixed a null check")
        self.finish_phase("round: fixed the fix")

        self.assertNotEqual(self.status()["state"], "stuck")
        self.assertEqual(self.interrupts(), [])
        *_, critique, wrap_up = self.prompts_sent()
        self.assertTrue(critique.startswith("AFK phase: critique"), critique)
        self.assertTrue(wrap_up.startswith("AFK phase: wrap-up"), wrap_up)
        self.assertIn("over 2 critique rounds", wrap_up)

    def test_a_ticket_over_its_ticket_minutes_is_told_to_wrap_up_though_its_phase_has_just_begun(self):
        self.limits(ticket_minutes=30)
        self.now += 20 * 60
        self.finish_phase()
        self.now += 9 * 60
        self.afk("queue", "tick")
        self.assertEqual(len(self.prompts_sent()), 1)

        self.now += 2 * 60
        self.afk("queue", "tick")

        self.assertEqual(len(self.interrupts()), 1)
        verify, wrap_up = self.prompts_sent()
        self.assertTrue(wrap_up.startswith("AFK phase: wrap-up"), wrap_up)
        self.assertIn("over 30m on the ticket", wrap_up)

    def test_ticket_minutes_defaults_to_four_hours(self):
        self.limits(phase_minutes=1000, idle_minutes=1000)
        self.now += 239 * 60
        self.afk("queue", "tick")
        self.assertEqual(self.prompts_sent(), [])

        self.now += 2 * 60
        self.afk("queue", "tick")

        [wrap_up] = self.prompts_sent()
        self.assertIn("over 240m on the ticket", wrap_up)

    def test_a_worker_that_reports_done_but_never_stops_is_abandoned_after_the_grace_period(self):
        self.limits(phase_minutes=5, wrap_up_minutes=10)
        self.now += 6 * 60
        self.afk("queue", "tick")
        self.afk("report", "done", "Wrapped up", env=self.worker_env())  # but it carries on, hung in a tool call

        self.now += 11 * 60
        self.afk("queue", "tick")

        self.assertFalse((self.state_home / "afk" / ".queue" / "workers" / "12").exists())
        self.assertIn("abandoned  acme/widgets#12", self.afk("queue"))

    def test_a_worker_whose_agent_has_exited_gets_no_wrap_up_prompt_and_is_abandoned_after_the_grace_period(self):
        self.limits(idle_minutes=10, wrap_up_minutes=10)
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%5")] = "bash\n"
        self.now += 11 * 60
        self.afk("queue", "tick")
        self.assertEqual(self.prompts_sent(), [])
        self.assertNotEqual(self.status()["state"], "stuck")

        self.now += 11 * 60
        self.afk("queue", "tick")

        self.assertFalse((self.state_home / "afk" / ".queue" / "workers" / "12").exists())
        self.assertIn("abandoned  acme/widgets#12", self.afk("queue"))

    def test_the_wrap_up_prompt_is_sent_only_once(self):
        self.limits(phase_minutes=5, idle_minutes=5, wrap_up_minutes=30)
        self.now += 6 * 60
        self.afk("queue", "tick")

        self.now += 6 * 60
        self.afk("queue", "tick")

        self.assertEqual(len(self.interrupts()), 1)
        self.assertEqual(len(self.prompts_sent()), 1)
        self.assertNotEqual(self.status()["state"], "stuck")

    def test_a_worker_that_hasnt_wrapped_up_by_the_end_of_the_grace_period_is_abandoned_and_the_next_ticket_starts(self):
        self.limits(phase_minutes=5, wrap_up_minutes=10)
        worktree = str(self.state_home / "afk" / ".queue" / "worktrees" / "12")
        self.run_fake.responses[("git", "-C", worktree, "log")] = "3f2a9c1 Half an export\n"  # never pushed
        self.enqueue(13)
        self.now += 6 * 60
        self.afk("queue", "tick")
        self.now += 9 * 60
        self.afk("queue", "tick")
        self.assertTrue((self.state_home / "afk" / ".queue" / "workers" / "12").exists())

        self.now += 2 * 60
        output = self.afk("queue", "tick")

        self.assertFalse((self.state_home / "afk" / ".queue" / "workers" / "12").exists())
        self.assertEqual(self.run_fake.find("tmux", "kill-window"), [["tmux", "kill-window", "-t", "%5"]])
        self.assertEqual(self.run_fake.find("git", "-C", str(self.repo), "worktree", "remove"),
                         [["git", "-C", str(self.repo), "worktree", "remove", "--force", worktree]])
        self.assertEqual(self.run_fake.find("git", "-C", str(self.repo), "branch", "-D"),
                         [["git", "-C", str(self.repo), "branch", "-D", "12-widget-export"]])
        [comment] = self.run_fake.find("gh", "issue", "comment")
        self.assertEqual(comment[3:6], ["12", "--repo", "acme/widgets"])
        self.assertIn("over 5m in implement", comment[comment.index("--body") + 1])
        self.assertEqual(self.run_fake.find("gh", "issue", "close"), [])
        self.assertEqual([c for c in self.run_fake.find("gh", "issue", "edit") if "--remove-assignee" in c], [])
        [notification] = self.desktop_notifications()
        self.assertIn("acme/widgets#12 abandoned", notification)
        self.assertIn("\a", output)
        self.assertRegex(self.afk("queue"), r"abandoned  acme/widgets#12  Widget export  .*over 5m in implement")
        self.assertEqual([add[6] for add in self.worktree_adds()], ["12-widget-export", "13-widget-13"])

    def test_a_worker_that_reports_done_within_the_grace_period_ends_as_drafted_though_its_draft_is_found_after(self):
        self.limits(phase_minutes=5, wrap_up_minutes=10)
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%6")] = "bash\n"  # its shell pane is idle
        self.run_fake.responses[("gh", "pr", "view")] = pr_view("OPEN", url="https://github.com/acme/widgets/pull/7")
        self.now += 6 * 60
        self.afk("queue", "tick")
        self.now += 9 * 60 + 40
        self.afk("queue", "tick")  # polls its PR while it's still wrapping up
        self.now += 10
        self.finish_phase("Wrapped up: export half done")
        self.now += 30
        self.afk("queue", "tick")  # past the grace period, but its PR isn't polled again until a minute has passed
        self.assertTrue((self.state_home / "afk" / ".queue" / "workers" / "12").exists())

        self.now += 30
        output = self.afk("queue", "tick")

        self.assertIn("drafted  acme/widgets#12  Widget export  https://github.com/acme/widgets/pull/7", self.afk("queue"))
        [comment] = self.run_fake.find("gh", "issue", "comment")
        self.assertIn("draft PR", comment[comment.index("--body") + 1])
        self.assertNotIn("\a", output)


class QueueAbandonTest(QueueWorkerTestCase):
    """`afk dequeue <ticket> --abandon`: the engineer gives up on the running ticket."""

    def test_dequeue_refuses_the_running_ticket_and_names_abandon(self):
        output = self.queue_fails("dequeue", "acme/widgets#12")

        self.assertIn("acme/widgets#12 is running", output)
        self.assertIn("afk dequeue acme/widgets#12 --abandon", output)
        self.assertTrue((self.state_home / "afk" / ".queue" / "workers" / "12").exists())
        self.assertEqual(self.run_fake.find("tmux", "kill-window"), [])

    def test_abandon_stops_and_cleans_up_the_running_worker_keeping_what_it_pushed_and_the_next_ticket_starts(self):
        worktree = str(self.state_home / "afk" / ".queue" / "worktrees" / "12")
        self.run_fake.responses[("git", "-C", worktree, "log")] = "3f2a9c1 Half an export\n"  # never pushed
        self.enqueue(13)

        output = self.afk("dequeue", self.URL, "--abandon")

        self.assertIn("abandoned acme/widgets#12", output)
        self.assertNotIn("\a", output)  # the engineer abandoned it, so there's no one to call
        self.assertFalse((self.state_home / "afk" / ".queue" / "workers" / "12").exists())
        self.assertEqual(self.run_fake.find("tmux", "kill-window"), [["tmux", "kill-window", "-t", "%5"]])
        self.assertEqual(self.run_fake.find("git", "-C", str(self.repo), "worktree", "remove"),
                         [["git", "-C", str(self.repo), "worktree", "remove", "--force", worktree]])
        self.assertEqual(self.run_fake.find("git", "-C", str(self.repo), "branch", "-D"),
                         [["git", "-C", str(self.repo), "branch", "-D", "12-widget-export"]])
        self.assertEqual(self.run_fake.find("git", "-C", str(self.repo), "push"), [])
        [comment] = self.run_fake.find("gh", "issue", "comment")
        self.assertIn("afk dequeue --abandon", comment[comment.index("--body") + 1])
        self.assertRegex(self.afk("queue"), r"abandoned  acme/widgets#12  Widget export  .*afk dequeue --abandon")
        self.afk("queue", "tick")
        self.assertEqual([add[6] for add in self.worktree_adds()], ["12-widget-export", "13-widget-13"])

    def test_an_abandon_whose_cleanup_fails_partway_is_finished_by_the_next_tick(self):
        removes = []

        def remove_fails_once(cmd):
            removes.append(cmd)
            if len(removes) == 1:
                raise subprocess.CalledProcessError(1, cmd, stderr="fatal: busy")
            return git(cmd)

        self.run_fake.responses[("git", "-C", str(self.repo), "worktree", "remove")] = remove_fails_once
        self.enqueue(13)
        self.queue_fails("dequeue", self.URL, "--abandon")

        self.afk("queue", "tick")

        self.assertFalse((self.state_home / "afk" / ".queue" / "workers" / "12").exists())
        self.assertEqual(len(self.run_fake.find("gh", "issue", "comment")), 1)
        self.assertEqual([add[6] for add in self.worktree_adds()], ["12-widget-export", "13-widget-13"])

    def test_an_abandoned_ticket_can_be_enqueued_again(self):
        self.afk("dequeue", self.URL, "--abandon")

        self.assertIn("enqueued acme/widgets#12 at position 1", self.afk("enqueue", self.URL))

    def test_abandon_on_a_waiting_ticket_just_takes_it_off_the_queue(self):
        self.enqueue(13)

        self.assertIn("removed acme/widgets#13", self.afk("dequeue", "acme/widgets#13", "--abandon"))
        self.assertNotIn("acme/widgets#13", self.afk("queue"))

    def test_an_unknown_option_fails_with_usage(self):
        self.assertIn("usage: afk dequeue", self.queue_fails("dequeue", "acme/widgets#12", "--force"))


class QueueBranchTest(QueueTickTestCase):
    """A queue ticket's branch is the one its tracker links to it, so a later pass finds it whatever it's named."""

    def gadgets(self):
        gadgets = self.clone(self.tmp / "gadgets", "git@github.com:acme/gadgets.git")
        (gadgets / "docs" / "agents").mkdir(parents=True)
        (gadgets / "docs" / "agents" / "afk.md").write_text('+++\nbase = "develop"\n+++\n\n# AFK\n')
        self.clones["acme/gadgets"] = gadgets
        self.on_origin(gadgets)
        return gadgets

    def test_a_ticket_picked_up_for_the_first_time_gets_a_branch_github_names_and_links_to_it(self):
        self.enqueue(12)

        self.afk("queue", "tick")

        [develop] = self.run_fake.find("gh", "issue", "develop")
        self.assertEqual(develop, ["gh", "issue", "develop", "12", "--repo", "acme/widgets", "--base", "main"])
        # In the clone, where gh records the branch's base for `gh pr create`, which fails outside a checkout.
        self.assertEqual(self.run_fake.cwds[self.run_fake.calls.index(develop)], str(self.repo))
        [add] = self.worktree_adds()
        self.assertEqual(add[5:7], ["-b", "12-widget-export"])
        # From base as GitHub made the branch, so the worker's first push is a fast-forward of it.
        self.assertEqual(add[-1], "origin/main")
        self.assertTrue(shlex.split(self.run_fake.find("tmux", "send-keys")[-1][4])[-1].startswith("AFK phase: implement"))

    def test_a_branch_the_engineer_linked_to_the_ticket_is_the_one_its_worker_takes(self):
        self.link(12, "export-spike")
        self.enqueue(12)

        self.afk("queue", "tick")

        self.assertEqual(self.run_fake.find("gh", "issue", "develop"), [])
        self.assertEqual(self.worktree_adds()[0][5:7], ["-b", "export-spike"])

    def test_a_branch_linked_to_a_ticket_in_another_repo_is_never_taken_though_their_numbers_match(self):
        gadgets = self.gadgets()
        self.issue(12, "Gadget export", repo="acme/gadgets")
        # acme/widgets#12's branch in acme/gadgets, from a pass on it that spanned both repos.
        self.link(12, "12-widget-export", branch_repo="acme/gadgets")
        self.push("12-widget-export", clone=gadgets)
        self.run_fake.responses[("gh", "pr", "list")] = json.dumps([{"url": "https://github.com/acme/gadgets/pull/3"}])
        self.afk("enqueue", "https://github.com/acme/gadgets/issues/12")

        self.afk("queue", "tick")

        [add] = self.worktree_adds()
        self.assertEqual((add[2], add[6], add[-1]), (str(gadgets), "12-gadget-export", "origin/develop"))
        self.assertTrue(shlex.split(self.run_fake.find("tmux", "send-keys")[-1][4])[-1].startswith("AFK phase: implement"))

    def test_a_linked_branch_with_nothing_pushed_beyond_base_starts_afresh_from_base(self):
        self.link(12, "12-widget-export")
        self.push("12-widget-export", ahead=0)
        self.enqueue(12)

        self.afk("queue", "tick")

        self.assertEqual(self.worktree_adds()[0][-1], "origin/main")
        self.assertTrue(shlex.split(self.run_fake.find("tmux", "send-keys")[-1][4])[-1].startswith("AFK phase: implement"))

    def test_a_branch_github_fails_to_make_keeps_the_ticket_in_its_place_saying_why(self):
        self.link(99, "12-widget-export")  # the name GitHub suggests is another issue's branch
        self.enqueue(12)

        self.afk("queue", "tick")

        self.assertEqual(self.worktree_adds(), [])
        listing = self.afk("queue")
        self.assertIn("1. acme/widgets#12", listing)
        self.assertIn("Reference already exists", listing)


class QueueResumeTest(QueueWorkerTestCase):
    """acme/widgets#12 enqueued again and picked up once an earlier pass pushed its branch, 12-widget-export, and opened
    its draft a day before."""

    DRAFT = "https://github.com/acme/widgets/pull/7"

    def before_pick_up(self):
        self.link(12, "12-widget-export")
        self.run_fake.responses[("git", "-C", str(self.repo), "ls-remote")] = "3f2a9c1\trefs/heads/12-widget-export\n"
        self.run_fake.responses[("git", "-C", str(self.repo), "rev-list")] = "2\n"  # commits beyond main
        self.run_fake.responses[("gh", "pr", "list")] = json.dumps([{"url": self.DRAFT}])
        self.run_fake.responses[("gh", "pr", "view")] = pr_view("OPEN", url=self.DRAFT, created=self.now - 86400)

    def launch_prompt(self):
        [send] = self.run_fake.find("tmux", "send-keys")
        return shlex.split(send[4])[-1]

    def test_the_worker_starts_on_the_pushed_branch_told_to_address_the_feedback_on_its_draft(self):
        fetches = self.run_fake.find("git", "-C", str(self.repo), "fetch")
        self.assertEqual(fetches, [["git", "-C", str(self.repo), "fetch", "origin",
                                    "+refs/heads/12-widget-export:refs/remotes/origin/12-widget-export",
                                    "+refs/heads/main:refs/remotes/origin/main"]])
        self.assertEqual(self.run_fake.find("gh", "issue", "develop"), [])  # its branch is linked already
        [add] = self.worktree_adds()
        self.assertEqual(add[5:7], ["-b", "12-widget-export"])
        self.assertEqual(add[-1], "origin/12-widget-export")
        prompt = self.launch_prompt()
        self.assertTrue(prompt.startswith("AFK phase: resume"), prompt)
        self.assertIn(self.DRAFT, prompt)
        self.assertIn(self.URL, prompt)
        self.assertNotIn("{{", prompt)

    def test_its_earlier_draft_still_open_once_it_is_done_ends_the_ticket_as_drafted_again(self):
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%6")] = "bash\n"  # its shell pane is idle
        for _ in range(4):
            self.finish_phase()
        self.now += 60

        self.finish_phase("Feedback addressed, questions refreshed")

        self.assertFalse((self.state_home / "afk" / ".queue" / "workers" / "12").exists())
        [comment] = self.run_fake.find("gh", "issue", "comment")
        self.assertIn(self.DRAFT, comment[comment.index("--body") + 1])
        self.assertIn(f"drafted  acme/widgets#12  Widget export  {self.DRAFT}", self.afk("queue"))
        self.assertIn(f"acme/widgets#12 draft PR updated: {self.DRAFT}", self.desktop_notifications()[-1])

    def test_the_comment_on_the_ticket_says_its_draft_was_updated_not_opened(self):
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%6")] = "bash\n"  # its shell pane is idle
        for _ in range(5):
            self.finish_phase()

        [comment] = self.run_fake.find("gh", "issue", "comment")
        self.assertEqual(comment[comment.index("--body") + 1], f"afk updated its draft PR for this ticket: {self.DRAFT}")


class QueueResumeWithoutDraftTest(QueueResumeTest):
    """acme/widgets#12 picked up again once an earlier pass pushed its branch, its draft since closed by the engineer."""

    def before_pick_up(self):
        super().before_pick_up()
        self.run_fake.responses[("gh", "pr", "list")] = "[]"
        self.run_fake.responses[("gh", "pr", "view")] = pr_view("CLOSED", url=self.DRAFT, created=self.now - 86400)

    def test_the_worker_starts_on_the_pushed_branch_with_no_draft_to_name(self):
        [add] = self.worktree_adds()
        self.assertEqual(add[-1], "origin/12-widget-export")
        prompt = self.launch_prompt()
        self.assertTrue(prompt.startswith("AFK phase: resume"), prompt)
        self.assertNotIn(self.DRAFT, prompt)
        self.assertNotIn("{{", prompt)

    def test_the_closed_draft_is_none_of_its_own_so_it_ends_once_it_opens_a_new_one(self):
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%6")] = "bash\n"  # its shell pane is idle
        for _ in range(5):
            self.finish_phase()
        self.assertEqual(self.status()["message"], afk.NO_PR)  # neither ended as closed nor taken as its draft
        new = "https://github.com/acme/widgets/pull/8"
        self.run_fake.responses[("gh", "pr", "view")] = pr_view("OPEN", url=new)
        self.now += 60

        self.afk("queue", "tick")

        [comment] = self.run_fake.find("gh", "issue", "comment")
        self.assertEqual(comment[comment.index("--body") + 1], f"afk opened a draft PR for this ticket: {new}")

    test_its_earlier_draft_still_open_once_it_is_done_ends_the_ticket_as_drafted_again = None
    test_the_comment_on_the_ticket_says_its_draft_was_updated_not_opened = None
    test_the_worker_starts_on_the_pushed_branch_told_to_address_the_feedback_on_its_draft = None


class QueueResumeFailureTest(QueueTickTestCase):
    def test_a_ticket_whose_branch_cannot_be_looked_up_keeps_its_place_and_is_tried_again_a_poll_later(self):
        def unreachable(cmd):
            raise subprocess.CalledProcessError(128, cmd, stderr="fatal: Could not read from remote repository.")

        self.link(12, "12-widget-export")
        self.run_fake.responses[("git", "-C", str(self.repo), "ls-remote")] = unreachable
        self.enqueue(12)

        self.afk("queue", "tick")

        self.assertEqual(self.worktree_adds(), [])
        self.assertIn("1. acme/widgets#12", self.afk("queue"))
        self.assertIn("Could not read from remote repository", self.afk("queue"))
        self.run_fake.responses[("git", "-C", str(self.repo), "ls-remote")] = ""
        self.now += 60
        self.afk("queue", "tick")
        self.assertEqual([add[-1] for add in self.worktree_adds()], ["main"])


class QueueAndProjectsTest(QueueWorkerTestCase):
    def test_queue_workers_and_a_projects_workers_never_count_toward_each_others_max(self):
        self.ticket("03-ui", "Widget UI")
        self.run_fake.responses[("tmux", "show-options")] = "widgets\n"
        self.afk("init", str(self.scratch))
        with open(self.state_home / "afk" / "widgets" / "config.toml", "a") as config:
            config.write("\n[limits]\nmax_workers = 1\n")

        self.afk("start", "03")
        self.enqueue(13)
        (self.state_home / "afk" / ".queue" / "config.toml").write_text("[queue]\nmax_workers = 2\n")
        self.afk("queue", "tick")

        self.assertEqual([add[6] for add in self.worktree_adds()], ["12-widget-export", "afk/widgets-03", "13-widget-13"])

    def test_enqueueing_the_running_ticket_is_refused(self):
        output = self.queue_fails("enqueue", self.URL)

        self.assertIn("acme/widgets#12 is running in the queue", output)
        self.assertNotIn("1. acme/widgets#12", self.afk("queue"))


class QueueWatchTest(QueueTickTestCase):
    def watch(self, sleeps=1):
        """Run `afk queue watch` from the afk-queue session for `sleeps` ticks, until it's interrupted."""
        self.run_fake.responses[("tmux", "display-message", "-p", "#{session_name}")] = "afk-queue\n"
        out = io.StringIO()
        with mock.patch.object(afk.time, "sleep", side_effect=[None] * (sleeps - 1) + [KeyboardInterrupt]):
            code = afk.main(["queue", "watch"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out,
                            clock=lambda: self.now)
        self.assertEqual(code, 0, out.getvalue())
        return out.getvalue()

    def test_a_restarted_watcher_keeps_driving_the_running_queue_worker(self):
        self.enqueue(12)
        self.watch()
        env = {"AFK_QUEUE": "1", "AFK_TICKET": "12"}
        self.afk("report", "done", "Implemented", env=env)
        self.afk("hook", "stop", stdin=json.dumps({"session_id": "s", "hook_event_name": "Stop"}), env=env)
        loads = len(self.run_fake.find("tmux", "load-buffer"))

        output = self.watch()

        [load] = self.run_fake.find("tmux", "load-buffer")[loads:]
        self.assertTrue(self.run_fake.inputs[self.run_fake.calls.index(load)].startswith("AFK phase: verify"))
        self.assertIn("verify", output)
        self.assertEqual(len(self.worktree_adds()), 1)

    def test_the_watcher_runs_only_in_the_afk_queue_session_where_its_workers_open(self):
        output = self.queue_fails("queue", "watch")

        self.assertIn("afk-queue", output)
        self.assertIn("tmux new-session -s afk-queue", output)


class QueueWorkerRestrictionTest(QueueWorkerTestCase):

    def test_a_queue_worker_keeps_a_pr_mode_workers_deny_list(self):
        for command in ["git push --force origin 12-widget-export", "git push origin main", "gh pr merge 7 --squash",
                        "git worktree remove ../other", "afk drop 12 --discard"]:
            self.assertTrue(self.denied(command), command)

    def test_a_queue_worker_may_not_mark_its_pr_ready_wait_on_the_engineer_or_run_the_queue(self):
        bin = shlex.quote(afk.AFK_BIN)
        for command in ["gh pr ready", "gh pr ready 7", "afk report question 'Which database?'", "afk report question",
                        f"{bin} report blocked",
                        f"{bin} report blocked 'CI is down'", "afk enqueue https://github.com/acme/widgets/issues/13",
                        "afk dequeue acme/widgets#13", "afk queue", "afk queue pause", f"{bin} queue resume"]:
            self.assertTrue(self.denied(command), command)
        for command in ["afk report done 'Opened the draft'", "gh pr create --draft --base main", "gh pr view"]:
            self.assertFalse(self.denied(command), command)

    def test_a_queue_worker_may_edit_only_inside_its_worktree(self):
        worktree = self.state_home / "afk" / ".queue" / "worktrees" / "12"
        for tool, key, path in [("Edit", "file_path", str(self.repo / "widget.py")), ("Write", "file_path", "/etc/hosts"),
                                ("MultiEdit", "file_path", str(worktree / ".." / "13" / "widget.py")),
                                ("NotebookEdit", "notebook_path", str(self.tmp / "notes.ipynb"))]:
            decision = self.about_to(tool, **{key: path})
            self.assertEqual(decision["permissionDecision"], "deny", (tool, path))
            self.assertIn("outside your worktree", decision["permissionDecisionReason"])
        for tool, path in [("Edit", str(worktree / "widget.py")), ("Write", "docs/widgets.md")]:
            self.assertIsNone(self.about_to(tool, file_path=path), path)
        self.assertIsNone(self.about_to("Bash", command="git status"))

    def test_a_project_worker_is_not_held_to_its_worktree(self):
        self.ticket("03-ui", "Widget UI")
        self.run_fake.responses[("tmux", "show-options")] = "widgets\n"
        self.afk("init", str(self.scratch))
        self.afk("start", "03")

        self.assertIsNone(self.about_to("Edit", env={"AFK_PROJECT": "widgets", "AFK_TICKET": "03"},
                                        file_path=str(self.repo / "widget.py")))


class QueueWorktreeTestCase(QueueWorkerTestCase):
    """acme/widgets#12's queue worker, with a clone of acme/gadgets beside acme/widgets' for `afk worktree` to find."""

    def setUp(self):
        super().setUp()
        self.gadgets = self.clone(self.tmp / "gadgets", "git@github.com:acme/gadgets.git")
        self.clones["acme/gadgets"] = self.gadgets
        self.on_origin(self.gadgets)
        (self.gadgets / ".env").write_text("SECRET=1\n")
        config = self.gadgets / "docs" / "agents" / "afk.md"
        config.parent.mkdir(parents=True)
        config.write_text('+++\nbase = "develop"\nport_base = 5000\ncopy = [".env"]\nbootstrap = ["npm ci"]\n'
                          'reviewers = ["gadget-review --base {{target}}"]\n+++\n\n# AFK\n')

    @property
    def sibling(self):
        return self.state_home / "afk" / ".queue" / "worktrees" / "12@acme.gadgets"  # no owner name holds a dot


class QueueWorktreeTest(QueueWorktreeTestCase):
    def test_afk_worktree_adds_a_worktree_on_the_workers_branch_in_the_other_repos_clone_prepared_for_its_slot(self):
        output = self.afk("worktree", "acme/gadgets", env=self.worker_env())

        self.assertIn(["git", "-C", str(self.gadgets), "worktree", "add", "-b", "12-widget-export", str(self.sibling), "origin/develop"],
                      self.run_fake.calls)
        self.assertEqual((self.sibling / ".env").read_text(), "SECRET=1\n")
        [bootstrap] = self.run_fake.find("env", "AFK_SLOT=8")
        self.assertEqual(bootstrap, ["env", "AFK_SLOT=8", "AFK_PORT_BASE=5800", "sh", "-c", "npm ci"])
        self.assertEqual(self.run_fake.cwds[self.run_fake.calls.index(bootstrap)], str(self.sibling))
        self.assertIn(str(self.sibling), output)

    def test_the_branch_in_the_other_repo_is_made_and_linked_to_the_ticket_under_the_workers_branch_name(self):
        self.afk("worktree", "acme/gadgets", env=self.worker_env())

        develop = self.run_fake.find("gh", "issue", "develop")[-1]
        self.assertEqual(develop, ["gh", "issue", "develop", "12", "--repo", "acme/widgets", "--base", "develop",
                                   "--branch-repo", "acme/gadgets", "--name", "12-widget-export"])
        self.assertEqual(self.run_fake.cwds[self.run_fake.calls.index(develop)], str(self.gadgets))

    def test_a_branch_of_that_name_in_the_other_repo_that_is_another_tickets_is_refused_adding_no_worktree(self):
        self.link(7, "12-widget-export", branch_repo="acme/gadgets")

        out = io.StringIO()
        code = afk.main(["worktree", "acme/gadgets"], run=self.run_fake, env={**self.env, **self.worker_env()},
                        stdin=io.StringIO(), stdout=out, clock=lambda: self.now)

        self.assertEqual(code, 1)
        self.assertIn("Reference already exists", out.getvalue())
        self.assertFalse(self.sibling.exists())
        self.assertEqual([c for c in self.worktree_adds() if c[2] == str(self.gadgets)], [])

    def test_it_prints_the_slot_env_the_other_repos_dev_server_runs_with(self):
        output = self.afk("worktree", "acme/gadgets", env=self.worker_env())

        self.assertIn("export AFK_SLOT=8 AFK_PORT_BASE=5800", output)

    def test_the_worker_is_denied_pushing_to_the_other_repos_base_too(self):
        self.assertFalse(self.denied("git push origin develop"))

        self.afk("worktree", "acme/gadgets", env=self.worker_env())

        for command in ["git push origin develop", "git push origin HEAD:develop", "git push origin main",
                        "git push --force origin 12-widget-export", "gh pr ready 3"]:
            self.assertTrue(self.denied(command), command)
        self.assertFalse(self.denied("git push -u origin HEAD"))

    def test_a_worker_cleaned_up_while_it_ran_has_the_new_worktree_and_branch_removed(self):
        def abandoned_meanwhile(cmd):
            shutil.rmtree(self.state_home / "afk" / ".queue" / "workers" / "12")
            return ""

        self.run_fake.responses[("env", "AFK_SLOT=8")] = abandoned_meanwhile
        out = io.StringIO()
        code = afk.main(["worktree", "acme/gadgets"], run=self.run_fake, env={**self.env, **self.worker_env()},
                        stdin=io.StringIO(), stdout=out, clock=lambda: self.now)

        self.assertEqual(code, 1)
        self.assertIn("cleaned up", out.getvalue())
        self.assertFalse(self.sibling.exists())
        self.assertIn(["git", "-C", str(self.gadgets), "branch", "-D", "12-widget-export"], self.run_fake.calls)

    def gadgets_adds(self):
        return [add for add in self.worktree_adds() if add[2] == str(self.gadgets)]

    def test_running_it_again_for_a_repo_it_has_a_worktree_in_just_names_that_worktree(self):
        self.afk("worktree", "acme/gadgets", env=self.worker_env())

        self.assertIn(str(self.sibling), self.afk("worktree", "ACME/Gadgets", env=self.worker_env()))
        self.assertIn(str(self.state_home / "afk" / ".queue" / "worktrees" / "12"),
                      self.afk("worktree", "acme/widgets", env=self.worker_env()))
        self.assertEqual(len(self.gadgets_adds()), 1)
        self.assertEqual(len(self.worktree_adds()), 2)

    def test_a_bootstrap_that_fails_removes_the_new_worktree_and_branch_so_a_retry_works(self):
        def bootstrap_fails(cmd):
            raise subprocess.CalledProcessError(1, cmd, stderr="npm ERR! network")

        self.run_fake.responses[("env", "AFK_SLOT=8")] = bootstrap_fails
        out = io.StringIO()
        code = afk.main(["worktree", "acme/gadgets"], run=self.run_fake, env={**self.env, **self.worker_env()},
                        stdin=io.StringIO(), stdout=out, clock=lambda: self.now)

        self.assertEqual(code, 1)
        self.assertIn("npm ERR! network", out.getvalue())
        self.assertFalse(self.sibling.exists())
        self.assertIn(["git", "-C", str(self.gadgets), "branch", "-D", "12-widget-export"], self.run_fake.calls)
        del self.run_fake.responses[("env", "AFK_SLOT=8")]
        self.afk("worktree", "acme/gadgets", env=self.worker_env())
        self.assertEqual(len(self.gadgets_adds()), 2)

    def test_every_phase_prompt_after_it_names_each_worktree_with_its_repo_and_base(self):
        self.afk("worktree", "acme/gadgets", env=self.worker_env())

        for _ in range(4):
            self.finish_phase()

        prompts = self.prompts_sent()
        self.assertEqual([p.splitlines()[0].split(" — ")[0] for p in prompts],
                         ["AFK phase: verify", "AFK phase: prepr", "AFK phase: critique", "AFK phase: draft"])
        own = self.state_home / "afk" / ".queue" / "worktrees" / "12"
        for prompt in prompts:
            self.assertIn(f"`acme/widgets` (base `main`, `AFK_PORT_BASE=4800`): {own}", prompt)
            self.assertIn(f"`acme/gadgets` (base `develop`, `AFK_PORT_BASE=5800`): {self.sibling}", prompt)
            self.assertIn("12-widget-export", prompt)
            self.assertNotIn("{{", prompt)

    def test_the_worker_may_edit_inside_its_worktree_in_the_other_repo_but_still_nowhere_else(self):
        self.afk("worktree", "acme/gadgets", env=self.worker_env())

        about_to = lambda path: self.about_to("Edit", file_path=path)
        self.assertIsNone(about_to(str(self.sibling / "gadget.py")))
        self.assertIsNone(about_to(str(self.state_home / "afk" / ".queue" / "worktrees" / "12" / "widget.py")))
        for path in [str(self.gadgets / "gadget.py"), str(self.sibling.with_name("13@acme.gadgets") / "gadget.py")]:
            self.assertEqual(about_to(path)["permissionDecision"], "deny", path)

    def test_abandoning_the_ticket_removes_its_worktree_and_local_branch_in_the_other_repo_too(self):
        self.afk("worktree", "acme/gadgets", env=self.worker_env())
        self.run_fake.responses[("git", "-C", str(self.sibling), "log")] = "3f2a9c1 Half a gadget\n"  # never pushed

        self.afk("dequeue", self.URL, "--abandon")

        self.assertEqual(self.run_fake.find("git", "-C", str(self.gadgets), "worktree", "remove"),
                         [["git", "-C", str(self.gadgets), "worktree", "remove", "--force", str(self.sibling)]])
        self.assertEqual(self.run_fake.find("git", "-C", str(self.gadgets), "branch", "-D"),
                         [["git", "-C", str(self.gadgets), "branch", "-D", "12-widget-export"]])
        self.assertFalse((self.state_home / "afk" / ".queue" / "workers" / "12").exists())

    def test_the_critique_prompt_gives_each_other_repos_reviewers_over_the_diff_against_its_own_base(self):
        self.afk("worktree", "acme/gadgets", env=self.worker_env())

        for _ in range(3):
            self.finish_phase()

        verify, prepr, critique = self.prompts_sent()
        self.assertIn("`gadget-review --base origin/develop`", critique)
        self.assertNotIn("gadget-review", verify)

    def test_a_ticket_in_one_repo_gets_no_list_of_worktrees(self):
        self.finish_phase()

        self.assertNotIn(str(self.state_home / "afk" / ".queue" / "worktrees" / "12"), self.prompts_sent()[-1])


class QueueSiblingDraftTest(QueueWorktreeTestCase):
    """A queue worker with a worktree in acme/gadgets too, that reports done in the draft phase."""

    DRAFTS = {"widgets": "https://github.com/acme/widgets/pull/7", "gadgets": "https://github.com/acme/gadgets/pull/3"}

    def setUp(self):
        super().setUp()
        self.afk("worktree", "acme/gadgets", env=self.worker_env())
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%6")] = "bash\n"  # its shell pane is idle
        for _ in range(4):
            self.finish_phase()
        self.prs = dict(self.DRAFTS)
        self.run_fake.responses[("gh", "pr", "view")] = self.pr_view
        self.now += 60

    def pr_view(self, cmd):
        """Each clone's PR for the branch, by the clone `gh` runs in."""
        url = self.prs.get(Path(self.run_fake.cwds[-1]).name.replace("repo", "widgets"))
        return pr_view("OPEN", url=url) if url else no_pr(cmd)

    def test_cleanup_removes_the_worktree_and_local_branch_in_every_repo(self):
        self.finish_phase("Drafts open, CI green")

        for clone, worktree in [(self.repo, self.state_home / "afk" / ".queue" / "worktrees" / "12"), (self.gadgets, self.sibling)]:
            self.assertEqual(self.run_fake.find("git", "-C", str(clone), "worktree", "remove"),
                             [["git", "-C", str(clone), "worktree", "remove", "--force", str(worktree)]])
            self.assertEqual(self.run_fake.find("git", "-C", str(clone), "branch", "-D"),
                             [["git", "-C", str(clone), "branch", "-D", "12-widget-export"]])
        self.assertFalse((self.state_home / "afk" / ".queue" / "workers" / "12").exists())

    def test_the_ticket_comment_the_outcome_and_the_notification_link_the_draft_in_every_repo(self):
        self.finish_phase("Drafts open, CI green")

        [comment] = self.run_fake.find("gh", "issue", "comment")
        for url in self.DRAFTS.values():
            self.assertIn(url, comment[comment.index("--body") + 1])
            self.assertIn(url, self.desktop_notifications()[-1])
        self.assertIn(f"drafted  acme/widgets#12  Widget export  {self.DRAFTS['widgets']} {self.DRAFTS['gadgets']}",
                      self.afk("queue"))

    def test_a_repo_whose_branch_has_no_pr_goes_unlinked(self):
        del self.prs["gadgets"]

        self.finish_phase("Draft open")

        [comment] = self.run_fake.find("gh", "issue", "comment")
        self.assertIn(self.DRAFTS["widgets"], comment[comment.index("--body") + 1])
        self.assertNotIn("gadgets", comment[comment.index("--body") + 1])
        self.assertIn(f"drafted  acme/widgets#12  Widget export  {self.DRAFTS['widgets']}\n", self.afk("queue") + "\n")

    def test_a_cleanup_that_failed_after_deleting_its_own_branch_finishes_without_a_notification(self):
        removes, deletes = [], []

        def sibling_remove_fails_once(cmd):
            removes.append(cmd)
            if len(removes) == 1:
                raise subprocess.CalledProcessError(1, cmd, stderr="fatal: busy")
            return git(cmd)

        def branch_gone_once_deleted(cmd):
            deletes.append(cmd)
            if len(deletes) > 1:
                raise subprocess.CalledProcessError(1, cmd, stderr="error: branch '12-widget-export' not found.")
            return ""

        self.run_fake.responses[("git", "-C", str(self.gadgets), "worktree", "remove")] = sibling_remove_fails_once
        self.run_fake.responses[("git", "-C", str(self.repo), "branch", "-D")] = branch_gone_once_deleted
        self.afk("report", "done", "Drafts open", env=self.worker_env())
        self.afk("hook", "stop", stdin=json.dumps({"session_id": "s", "hook_event_name": "Stop"}), env=self.worker_env())
        self.queue_fails("queue", "tick")

        output = self.afk("queue", "tick")

        self.assertFalse((self.state_home / "afk" / ".queue" / "workers" / "12").exists())
        self.assertFalse(self.sibling.exists())
        self.assertEqual([n for n in self.desktop_notifications() if "leaving branch" in n], [])
        self.assertNotIn("\a", output)

    def test_its_own_worktree_gone_still_leaves_the_other_repos_unpushed_commits_holding_cleanup_back(self):
        shutil.rmtree(self.state_home / "afk" / ".queue" / "worktrees" / "12")  # as by a cleanup that failed partway

        self.assertEqual(self.held_back_by_the_other_repo("log", "3f2a9c1 Add the gadget\n"),
                         "its acme/gadgets branch has unpushed commits")

    def held_back_by_the_other_repo(self, command, output):
        """Finish the draft with `git <command>` in the acme/gadgets worktree printing output, which holds cleanup back
        until it prints nothing; the status message while it's held."""
        self.run_fake.responses[("git", "-C", str(self.sibling), command)] = output

        self.finish_phase("Drafts open, CI green")

        self.assertEqual(self.status()["state"], "cleanup-pending")
        self.assertEqual(self.run_fake.find("tmux", "kill-window"), [])
        held = self.status()["message"]
        del self.run_fake.responses[("git", "-C", str(self.sibling), command)]
        self.now += 60
        self.afk("queue", "tick")
        self.assertFalse((self.state_home / "afk" / ".queue" / "workers" / "12").exists())
        return held

    def test_unpushed_commits_in_the_other_repos_worktree_leave_it_cleanup_pending(self):
        self.assertEqual(self.held_back_by_the_other_repo("log", "3f2a9c1 Add the gadget\n"),
                         "its acme/gadgets branch has unpushed commits")

    def test_uncommitted_changes_in_the_other_repos_worktree_leave_it_cleanup_pending(self):
        self.assertEqual(self.held_back_by_the_other_repo("status", " M gadget.py\n"),
                         "its acme/gadgets worktree has uncommitted changes")


class QueueSiblingResumeTest(QueueWorktreeTestCase):
    """acme/widgets#12's queue worker running `afk worktree acme/gadgets` on a second pass, once an earlier pass pushed
    12-widget-export to acme/gadgets and opened its draft there a day before."""

    DRAFT = "https://github.com/acme/gadgets/pull/3"

    def setUp(self):
        super().setUp()
        self.link(12, "12-widget-export", branch_repo="acme/gadgets")
        self.run_fake.responses[("git", "-C", str(self.gadgets), "ls-remote")] = "9b1e4d2\trefs/heads/12-widget-export\n"
        self.run_fake.responses[("git", "-C", str(self.gadgets), "rev-list")] = "1\n"  # commits beyond develop
        self.run_fake.responses[("gh", "pr", "list")] = json.dumps([{"url": self.DRAFT}])

    def test_the_worktree_starts_on_the_branch_as_last_pushed_to_that_repo(self):
        output = self.afk("worktree", "acme/gadgets", env=self.worker_env())

        self.assertEqual(self.run_fake.find("git", "-C", str(self.gadgets), "fetch"),
                         [["git", "-C", str(self.gadgets), "fetch", "origin",
                           "+refs/heads/12-widget-export:refs/remotes/origin/12-widget-export",
                           "+refs/heads/develop:refs/remotes/origin/develop"]])
        self.assertIn(["git", "-C", str(self.gadgets), "worktree", "add", "-b", "12-widget-export", str(self.sibling),
                       "origin/12-widget-export"], self.run_fake.calls)
        self.assertIn(self.DRAFT, output)

    def test_its_earlier_draft_there_is_linked_when_the_ticket_ends_drafted(self):
        self.afk("worktree", "acme/gadgets", env=self.worker_env())
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%6")] = "bash\n"  # its shell pane is idle
        for _ in range(4):
            self.finish_phase()
        widgets = "https://github.com/acme/widgets/pull/7"
        self.run_fake.responses[("gh", "pr", "view")] = lambda cmd: (
            pr_view("OPEN", url=self.DRAFT, created=self.now - 86400) if self.run_fake.cwds[-1] == str(self.gadgets)
            else pr_view("OPEN", url=widgets))
        self.now += 60

        self.finish_phase("Drafts open, CI green")

        [comment] = self.run_fake.find("gh", "issue", "comment")
        self.assertIn(f"{widgets}, {self.DRAFT}", comment[comment.index("--body") + 1])


class QueueWorktreeRefusalTest(QueueWorktreeTestCase):
    def refused(self, repo, env):
        out = io.StringIO()
        code = afk.main(["worktree", repo], run=self.run_fake, env={**self.env, **env}, stdin=io.StringIO(), stdout=out,
                        clock=lambda: self.now)
        self.assertEqual(code, 1, out.getvalue())
        self.assertEqual(len(self.worktree_adds()), 1)  # the worker's own
        return out.getvalue()

    def test_a_repo_with_no_clone_near_the_worker_nor_in_the_registry_is_refused(self):
        output = self.refused("acme/nowhere", self.worker_env())

        self.assertIn("no clone of acme/nowhere", output)
        self.assertIn("[repos]", output)

    def test_a_repo_with_no_afk_repo_config_is_refused(self):
        shutil.rmtree(self.gadgets / "docs")

        self.assertIn("run /afk-setup there first", self.refused("acme/gadgets", self.worker_env()))

    def test_it_is_refused_outside_a_queue_worker(self):
        for env in [{}, {"AFK_TICKET": "12"}, {"AFK_PROJECT": "widgets", "AFK_TICKET": "03"}, {"AFK_QUEUE": "1"},
                    {"AFK_QUEUE": "1", "AFK_TICKET": "13"}]:
            self.assertIn("a queue worker", self.refused("acme/gadgets", env), env)


class LinearQueueTestCase(QueueTickTestCase):
    """The queue with a standalone Linear issue, ENG-123, via a faked `linear` CLI."""

    URL = "https://linear.app/acme/issue/ENG-123/widget-export"

    def setUp(self):
        super().setUp()
        self.linear = FakeLinear()
        self.run_fake.responses[("linear", "api")] = self.linear.api
        self.run_fake.responses[("linear", "issue", "update")] = self.linear.update
        self.linear.issue("ENG-123", "Widget export", description="Repo: acme/widgets\n", child=False)


class LinearEnqueueTest(LinearQueueTestCase):
    def test_a_linear_issue_is_enqueued_by_its_identifier_for_the_repo_its_description_names(self):
        self.assertIn("enqueued ENG-123 at position 1", self.afk("enqueue", self.URL))

        self.assertIn("1. ENG-123  Widget export  (acme/widgets: type feature, agent claude, model sonnet, effort medium)",
                      self.afk("queue"))


class LinearQueueTickTest(LinearQueueTestCase):
    CLAIM = ["linear", "issue", "update", "ENG-123", "--assignee", "@me", "--no-input"]

    def test_a_tick_claims_a_linear_ticket_on_the_branch_linear_names_for_it(self):
        self.afk("enqueue", self.URL)

        self.afk("queue", "tick")

        self.assertEqual(self.run_fake.find("linear", "issue", "update"), [self.CLAIM])
        [add] = self.worktree_adds()
        self.assertEqual(add[5:7], ["-b", "me/eng-123-widget-export"])
        self.assertEqual(add[2], str(self.repo))
        [send] = self.run_fake.find("tmux", "send-keys")
        launch = shlex.split(send[4])
        self.assertIn("AFK_TICKET=eng-123", launch)
        self.assertIn(self.URL, launch[-1])
        self.assertTrue(self.afk("queue").startswith("running  ENG-123  Widget export  (implement, working)"))

    def test_a_linear_ticket_enqueued_again_resumes_on_its_branch_as_pushed(self):
        self.run_fake.responses[("git", "-C", str(self.repo), "ls-remote")] = "3f2a9c1\trefs/heads/me/eng-123-widget-export\n"
        self.run_fake.responses[("git", "-C", str(self.repo), "rev-list")] = "2\n"
        self.run_fake.responses[("gh", "pr", "list")] = json.dumps([{"url": "https://github.com/acme/widgets/pull/7"}])
        self.afk("enqueue", self.URL)

        self.afk("queue", "tick")

        [add] = self.worktree_adds()
        self.assertEqual(add[5:7], ["-b", "me/eng-123-widget-export"])
        self.assertEqual(add[-1], "origin/me/eng-123-widget-export")
        self.assertTrue(shlex.split(self.run_fake.find("tmux", "send-keys")[-1][4])[-1].startswith("AFK phase: resume"))

    def test_a_ticket_ended_while_it_waited_is_removed_with_a_notification(self):
        self.afk("enqueue", self.URL)
        self.linear.issues["ENG-123"]["state"] = "canceled"

        self.afk("queue", "tick")

        self.assertEqual(self.worktree_adds(), [])
        self.assertEqual(self.run_fake.find("linear", "issue", "update"), [])
        self.assertIn("ENG-123 removed from the queue: it was closed", self.desktop_notifications()[0])

    def test_a_ticket_assigned_to_someone_else_while_it_waited_is_removed_naming_them(self):
        self.afk("enqueue", self.URL)
        self.linear.issues["ENG-123"]["assignee"] = "Alice"

        self.afk("queue", "tick")

        self.assertEqual(self.worktree_adds(), [])
        self.assertIn("removed  ENG-123  Widget export  it was assigned to Alice", self.afk("queue"))

    def test_a_blocked_ticket_is_skipped_keeping_its_place_and_started_once_its_blocker_ends(self):
        self.linear.issue("ENG-100", "Widget schema", child=False)
        self.linear.blocked_by["ENG-123"] = ["ENG-100"]
        self.afk("enqueue", self.URL)

        self.afk("queue", "tick")

        self.assertEqual(self.worktree_adds(), [])
        self.assertEqual(self.desktop_notifications(), [])
        self.assertIn("1. ENG-123  Widget export  (acme/widgets: type feature, agent claude, model sonnet, effort medium)"
                      "  blocked by ENG-100", self.afk("queue"))

        self.linear.issues["ENG-100"]["state"] = "completed"
        self.now += 60
        self.afk("queue", "tick")
        self.assertEqual([add[6] for add in self.worktree_adds()], ["me/eng-123-widget-export"])


class LinearQueueDraftTest(LinearQueueTestCase):
    """ENG-123's queue worker through to its draft PR."""

    DRAFT = "https://github.com/acme/widgets/pull/7"

    def setUp(self):
        super().setUp()
        self.afk("enqueue", self.URL)
        self.afk("queue", "tick")
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%6")] = "bash\n"  # its shell pane is idle
        for _ in range(4):
            self.finish_phase()
        self.run_fake.responses[("gh", "pr", "view")] = pr_view("OPEN", url=self.DRAFT)
        self.now += 60

    def finish_phase(self, message="Done"):
        env = {"AFK_QUEUE": "1", "AFK_TICKET": "eng-123"}
        self.afk("report", "done", message, env=env)
        payload = {"session_id": "s", "transcript_path": str(self.tmp / "eng-123.jsonl"), "hook_event_name": "Stop"}
        self.afk("hook", "stop", stdin=json.dumps(payload), env=env)
        return self.afk("queue", "tick")

    def updates(self):
        """The issue updates afk made after claiming the ticket."""
        return self.run_fake.find("linear", "issue", "update")[1:]

    def test_the_draft_is_linked_from_the_linear_issue_whose_workflow_state_is_left_to_linear(self):
        self.finish_phase("Draft open, CI green")

        [comment] = self.run_fake.find("linear", "issue", "comment")
        self.assertEqual(comment[:5], ["linear", "issue", "comment", "add", "ENG-123"])
        self.assertIn(self.DRAFT, comment[comment.index("--body") + 1])
        self.assertEqual(self.updates(), [])
        self.assertEqual(self.run_fake.find("gh", "pr", "view")[-1][3], "me/eng-123-widget-export")
        self.assertEqual(self.run_fake.find("git", "-C", str(self.repo), "branch", "-D"),
                         [["git", "-C", str(self.repo), "branch", "-D", "me/eng-123-widget-export"]])
        self.assertIn(f"drafted  ENG-123  Widget export  {self.DRAFT}", self.afk("queue"))

    def test_renamed_before_it_is_enqueued_again_it_resumes_on_the_branch_its_last_pass_pushed(self):
        self.finish_phase("Draft open, CI green")
        self.linear.issues["ENG-123"]["title"] = "Widget export as CSV"  # its branchName now says so
        self.push("me/eng-123-widget-export")
        self.run_fake.responses[("gh", "pr", "list")] = json.dumps([{"url": self.DRAFT}])

        self.afk("enqueue", self.URL)
        self.afk("queue", "tick")

        add = self.worktree_adds()[-1]
        self.assertEqual(add[5:7], ["-b", "me/eng-123-widget-export"])
        self.assertEqual(add[-1], "origin/me/eng-123-widget-export")

    def test_a_pr_the_engineer_merges_mid_draft_leaves_the_issues_workflow_state_to_linear(self):
        self.run_fake.responses[("gh", "pr", "view")] = pr_view("MERGED", url=self.DRAFT)

        self.afk("queue", "tick")

        self.assertEqual(self.updates(), [])
        self.assertIn("ended  ENG-123  Widget export  its PR was merged", self.afk("queue"))

    def test_an_issue_the_engineer_ends_mid_draft_ends_the_ticket_without_a_comment_or_update(self):
        self.linear.issues["ENG-123"]["state"] = "canceled"

        self.afk("queue", "tick")

        self.assertEqual(self.run_fake.find("linear", "issue", "comment"), [])
        self.assertEqual(self.updates(), [])
        self.assertIn("ended  ENG-123  Widget export  its ticket was closed", self.afk("queue"))

    def test_a_pr_the_engineer_closes_mid_draft_unassigns_the_issue_and_sets_no_workflow_state(self):
        self.run_fake.responses[("gh", "pr", "view")] = pr_view("CLOSED", url=self.DRAFT)

        self.afk("queue", "tick")

        self.assertEqual(self.updates(), [["linear", "issue", "update", "ENG-123", "--unassign", "--no-input"]])


class LinearDequeueTest(LinearQueueTestCase):
    def test_a_linear_ticket_is_named_by_its_url_or_its_identifier_in_any_casing(self):
        self.linear.issue("ENG-124", "Widget import", description="Repo: acme/widgets\n", child=False)
        self.afk("enqueue", self.URL)
        self.afk("enqueue", "https://linear.app/acme/issue/ENG-124")

        self.assertIn("removed ENG-123 from the queue", self.afk("dequeue", "https://linear.app/acme/issue/eng-123/widget-export"))
        self.assertIn("removed ENG-124 from the queue", self.afk("dequeue", "eng-124"))
        self.assertIn("the queue is empty", self.afk("queue"))


class LinearEnqueueRefusalTest(LinearQueueTestCase):
    def refused(self, *argv):
        output = self.queue_fails("enqueue", self.URL, *argv)
        self.assertIn("the queue is empty", self.afk("queue"))
        return output

    def test_a_ticket_naming_no_repo_is_refused_unless_one_is_passed(self):
        self.linear.issue("ENG-123", "Widget export", child=False)

        self.assertIn("ENG-123 names no repo: give it a `Repo: owner/name` line, or pass --repo", self.refused())
        self.assertIn("enqueued ENG-123", self.afk("enqueue", self.URL, "--repo", "acme/widgets"))

    def test_an_ended_ticket_is_refused(self):
        for state in ("completed", "canceled"):
            self.linear.issue("ENG-123", "Widget export", state=state, description="Repo: acme/widgets\n", child=False)

            self.assertIn("ENG-123 is closed", self.refused())

    def test_a_ticket_assigned_to_someone_else_is_refused_naming_them(self):
        self.linear.issue("ENG-123", "Widget export", assignee="Alice", description="Repo: acme/widgets\n", child=False)

        self.assertIn("ENG-123 is assigned to Alice", self.refused())

    def test_a_ticket_assigned_to_me_is_accepted(self):
        self.linear.issue("ENG-123", "Widget export", assignee="Me", description="Repo: acme/widgets\n", child=False)

        self.assertIn("enqueued ENG-123", self.afk("enqueue", self.URL))

    def test_a_hitl_ticket_is_refused(self):
        self.linear.issue("ENG-123", "Widget export", description="Type: hitl\nRepo: acme/widgets\n", child=False)

        self.assertIn("ENG-123 is a hitl ticket", self.refused())

    def test_an_unknown_issue_is_refused(self):
        self.assertIn("no ticket 'ENG-404'", self.queue_fails("enqueue", "https://linear.app/acme/issue/ENG-404"))


class FrontierTest(AfkTestCase):
    def test_frontier_lists_open_tickets_whose_blockers_are_all_done(self):
        self.ticket("01-schema", "Widget schema", status="resolved")
        self.ticket("02-api", "Widget API", status="done", blocked_by="01")
        self.ticket("03-ui", "Widget UI", blocked_by="01, 02")
        self.ticket("04-search", "Widget search", blocked_by="03")
        self.ticket("05-export", "Widget export")
        self.ticket("06-import", "Widget import", status="claimed")
        self.afk("init", str(self.scratch))

        output = self.afk("frontier")

        self.assertEqual(output.splitlines(), ["03  Widget UI", "05  Widget export"])

    def test_numbers_in_blocker_prose_are_not_treated_as_tickets(self):
        self.ticket("01-auth", "Add 2FA login", status="done")
        self.ticket("02-keys", "Key rotation", blocked_by="None — needs 2 API keys")
        self.ticket("03-audit", "Audit log", blocked_by="01 — Add 2FA login")
        self.afk("init", str(self.scratch))

        output = self.afk("frontier")

        self.assertEqual(output.splitlines(), ["02  Key rotation", "03  Audit log"])


class StartTest(AfkTestCase):
    def setUp(self):
        super().setUp()
        self.ticket("03-ui", "Widget UI")
        self.afk("init", str(self.scratch))

    def launch_command(self):
        [send] = self.run_fake.find("tmux", "send-keys")
        self.assertEqual(send[:4], ["tmux", "send-keys", "-t", "%5"])
        self.assertEqual(send[-1], "Enter")
        return shlex.split(send[4])

    def test_start_creates_worktree_and_branch_named_from_ticket(self):
        self.afk("start", "03")

        [add] = self.run_fake.find("git", "-C", str(self.repo), "worktree", "add")
        self.assertEqual(add[5:7], ["-b", "afk/widgets-03"])
        self.assertEqual(add[7], str(self.project_dir / "worktrees" / "03"))

    def test_start_opens_named_window_split_agent_left_shell_right(self):
        self.afk("start", "03")

        worktree = str(self.project_dir / "worktrees" / "03")
        [window] = self.run_fake.find("tmux", "new-window")
        self.assertIn("-d", window)
        self.assertEqual(window[window.index("-t") + 1], "work:")
        self.assertEqual(window[window.index("-n") + 1], "03-widget-ui")
        self.assertEqual(window[window.index("-c") + 1], worktree)
        self.assertEqual(
            self.run_fake.find("tmux", "split-window"),
            [["tmux", "split-window", "-h", "-d", "-t", "%5", "-c", worktree, "-P", "-F", "#{pane_id}"]],
        )

    def test_start_launches_interactive_claude_in_auto_mode_with_generated_settings(self):
        self.afk("start", "03")

        launch = self.launch_command()
        self.assertIn("AFK_PROJECT=widgets", launch)
        self.assertIn("AFK_TICKET=03", launch)
        claude = launch[launch.index("claude") :]
        self.assertEqual(claude[1:3], ["--permission-mode", "auto"])
        self.assertEqual(claude[3], "--settings")
        self.assertTrue(Path(claude[4]).is_file())
        self.assertNotIn("--dangerously-skip-permissions", claude)
        self.assertNotIn("-p", claude)
        self.assertIn(str(self.scratch / "issues" / "03-ui.md"), claude[-1])
        self.assertIn('afk report', claude[-1])

    def test_worker_session_start_hook_firing_immediately_on_launch_is_recorded(self):
        def worker_starts(cmd):
            payload = {"session_id": "early", "transcript_path": "/t.jsonl", "hook_event_name": "SessionStart"}
            self.afk("hook", "session-start", stdin=json.dumps(payload), env={"AFK_PROJECT": "widgets", "AFK_TICKET": "03"})
            return ""

        self.run_fake.responses[("tmux", "send-keys")] = worker_starts
        self.afk("start", "03")

        status = json.loads((self.project_dir / "workers" / "03" / "status.json").read_text())
        self.assertEqual(status["session_id"], "early")
        self.assertEqual(status["state"], "working")

    def test_failed_start_rolls_back_window_worktree_and_branch(self):
        def split_fails(cmd):
            raise subprocess.CalledProcessError(1, cmd, stderr="no space for new pane")

        self.run_fake.responses[("tmux", "split-window")] = split_fails
        out = io.StringIO()
        code = afk.main(["start", "03"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out)

        self.assertNotEqual(code, 0)
        self.assertIn("no space for new pane", out.getvalue())
        worktree = str(self.project_dir / "worktrees" / "03")
        self.assertEqual(self.run_fake.find("tmux", "kill-window"), [["tmux", "kill-window", "-t", "%5"]])
        self.assertEqual(
            self.run_fake.find("git", "-C", str(self.repo), "worktree", "remove"),
            [["git", "-C", str(self.repo), "worktree", "remove", "--force", worktree]],
        )
        self.assertEqual(
            self.run_fake.find("git", "-C", str(self.repo), "branch"),
            [["git", "-C", str(self.repo), "branch", "-D", "afk/widgets-03"]],
        )
        self.assertFalse((self.project_dir / "workers" / "03").exists())

    def test_interrupted_start_rolls_back_and_can_be_retried(self):
        def interrupted(cmd):
            raise KeyboardInterrupt

        self.run_fake.responses[("tmux", "split-window")] = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self.afk("start", "03")

        self.assertEqual(len(self.run_fake.find("git", "-C", str(self.repo), "worktree", "remove")), 1)
        del self.run_fake.responses[("tmux", "split-window")]
        self.afk("start", "03")
        self.assertEqual(self.worker_state("03"), "working")

    def worker_state(self, ticket):
        return json.loads((self.project_dir / "workers" / ticket / "status.json").read_text())["state"]

    def test_starting_an_already_started_ticket_leaves_the_running_worker_alone(self):
        self.afk("start", "03")
        calls_before = len(self.run_fake.calls)

        out = io.StringIO()
        code = afk.main(["start", "03"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out)

        self.assertNotEqual(code, 0)
        self.assertIn("already started", out.getvalue())
        later = self.run_fake.calls[calls_before:]
        self.assertFalse([c for c in later if c[0] == "git" or c[:2] in (["tmux", "new-window"], ["tmux", "kill-window"])])
        self.assertTrue((self.project_dir / "workers" / "03" / "status.json").is_file())

    def test_start_refuses_a_worker_beyond_the_configured_max_until_one_reaches_review(self):
        for number, title in [("05-export", "Widget export"), ("07-import", "Widget import")]:
            self.ticket(number, title)
        with open(self.project_dir / "config.toml", "a") as config:
            config.write("\n[limits]\nmax_workers = 2\n")
        self.afk("start", "03")
        self.afk("start", "05")

        out = io.StringIO()
        code = afk.main(["start", "07"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out)

        self.assertNotEqual(code, 0)
        self.assertIn("max_workers", out.getvalue())
        self.assertFalse((self.project_dir / "workers" / "07").exists())
        self.assertEqual(len(self.run_fake.find("tmux", "new-window")), 2)

        status_path = self.project_dir / "workers" / "03" / "status.json"
        status = json.loads(status_path.read_text())
        status_path.write_text(json.dumps({**status, "phase": "review", "state": "review"}))
        self.afk("start", "07")
        self.assertEqual(self.worker_state("07"), "working")

    def test_concurrent_starts_cannot_both_slip_under_the_max(self):
        self.ticket("05-export", "Widget export")
        with open(self.project_dir / "config.toml", "a") as config:
            config.write("\n[limits]\nmax_workers = 1\n")
        codes = []

        def start_05():
            out = io.StringIO()
            codes.append(afk.main(["start", "05"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out))

        other = threading.Thread(target=start_05)

        def other_start_arrives_mid_setup(cmd):
            if not other.is_alive() and not codes:
                other.start()
                other.join(timeout=0.2)  # without a lock it passes the check now, before 03 is recorded
            return ""

        self.run_fake.responses[("git", "-C")] = other_start_arrives_mid_setup
        self.afk("start", "03")
        other.join()

        self.assertEqual(codes, [1])
        self.assertFalse((self.project_dir / "workers" / "05").exists())

    def test_a_hitl_worker_still_being_set_up_does_not_count_toward_the_max(self):
        self.ticket("04-spike", "Widget spike", type="hitl")
        with open(self.project_dir / "config.toml", "a") as config:
            config.write("\n[limits]\nmax_workers = 1\n")
        codes = []

        def afk_start_arrives_mid_hitl_setup(cmd):
            if not codes:
                codes.append(None)  # the nested start runs git too
                out = io.StringIO()
                codes[0] = afk.main(["start", "03"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out)
                codes.append(out.getvalue())
            return ""

        self.run_fake.responses[("git", "-C")] = afk_start_arrives_mid_hitl_setup
        self.afk("start", "04")

        self.assertEqual(codes[0], 0, codes[1])

    def settings(self):
        self.afk("start", "03")
        launch = self.launch_command()
        return json.loads(Path(launch[launch.index("--settings") + 1]).read_text())

    def test_settings_deny_force_push_push_to_base_pr_merge_and_worktree_removal(self):
        deny = self.settings()["permissions"]["deny"]

        for rule in [
            "Bash(git push *--force*)",
            "Bash(git push * main)",
            "Bash(git push * HEAD:main)",
            "Bash(gh pr merge *)",
            "Bash(git worktree remove *)",
        ]:
            self.assertIn(rule, deny)

    def test_settings_wire_status_hooks_to_afk_hook(self):
        hooks = self.settings()["hooks"]

        for event, arg in [("SessionStart", "session-start"), ("PreToolUse", "activity"), ("Stop", "stop"),
                           ("Notification", "notification"), ("UserPromptSubmit", "prompt")]:
            command = shlex.split(hooks[event][0]["hooks"][0]["command"])
            self.assertEqual(command[-2:], ["hook", arg])
            self.assertTrue(os.access(command[0], os.X_OK), command[0])


class RepoConfigStartTest(AfkTestCase):
    def setUp(self):
        super().setUp()
        self.ticket("03-ui", "Widget UI")
        self.afk("init", str(self.scratch))
        self.worktree = self.project_dir / "worktrees" / "03"

    def test_start_copies_configured_gitignored_files_into_the_worktree(self):
        (self.repo / ".env").write_text("SECRET=1\n")
        (self.repo / "config").mkdir()
        (self.repo / "config" / "local.yml").write_text("db: dev\n")
        self.repo_config('copy = [".env", "config/local.yml"]')

        self.afk("start", "03")

        self.assertEqual((self.worktree / ".env").read_text(), "SECRET=1\n")
        self.assertEqual((self.worktree / "config" / "local.yml").read_text(), "db: dev\n")

    def test_worktree_branches_from_the_repos_base_branch_which_workers_may_not_push_to(self):
        self.repo_config('base = "develop"')

        self.afk("start", "03")

        [add] = self.run_fake.find("git", "-C", str(self.repo), "worktree", "add")
        self.assertEqual(add[-1], "develop")
        [send] = self.run_fake.find("tmux", "send-keys")
        launch = shlex.split(send[4])
        deny = json.loads(Path(launch[launch.index("--settings") + 1]).read_text())["permissions"]["deny"]
        self.assertIn("Bash(git push * develop)", deny)
        self.assertNotIn("Bash(git push * main)", deny)

    def bootstraps(self):
        """(shell command, cwd) of each bootstrap command run so far."""
        return [(cmd[-1], cwd) for cmd, cwd in zip(self.run_fake.calls, self.run_fake.cwds) if cmd[-3:-1] == ["sh", "-c"]]

    def test_start_runs_bootstrap_commands_in_order_in_the_worktree(self):
        self.repo_config('bootstrap = ["npm ci", "make db"]')

        self.afk("start", "03")

        self.assertEqual(self.bootstraps(), [("npm ci", str(self.worktree)), ("make db", str(self.worktree))])

    def test_failed_bootstrap_rolls_back_the_start(self):
        def npm_fails(cmd):
            if cmd[-1] == "npm ci":
                raise subprocess.CalledProcessError(1, cmd, stderr="npm ERR! missing lockfile")
            return ""

        self.run_fake.responses[("sh",)] = npm_fails
        self.run_fake.responses[("env",)] = npm_fails
        self.repo_config('bootstrap = ["npm ci"]')
        out = io.StringIO()

        code = afk.main(["start", "03"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out)

        self.assertNotEqual(code, 0)
        self.assertIn("missing lockfile", out.getvalue())
        self.assertEqual(len(self.run_fake.find("git", "-C", str(self.repo), "worktree", "remove")), 1)
        self.assertEqual(self.run_fake.find("tmux", "send-keys"), [])
        self.assertFalse((self.project_dir / "workers" / "03").exists())


class LandingStartTest(AfkTestCase):
    def setUp(self):
        super().setUp()
        self.ticket("03-ui", "Widget UI")

    def worktree_base(self):
        [add] = self.run_fake.find("git", "-C", str(self.repo), "worktree", "add")
        return add[-1]

    def status(self):
        return json.loads((self.project_dir / "workers" / "03" / "status.json").read_text())

    def test_a_local_mode_worktree_branches_from_the_integration_branch_and_its_status_records_it(self):
        self.afk("init", str(self.scratch), "--land", "local", "--target", "widgets-v2")

        self.afk("start", "03")

        self.assertEqual(self.worktree_base(), "widgets-v2")
        self.assertEqual((self.status()["landing"], self.status()["target"]), ("local", "widgets-v2"))

    def test_a_local_mode_project_without_a_target_lands_on_the_repos_base(self):
        self.repo_config('base = "develop"')
        self.afk("init", str(self.scratch), "--land", "local")

        self.afk("start", "03")

        self.assertEqual(self.worktree_base(), "develop")
        self.assertEqual((self.status()["landing"], self.status()["target"]), ("local", "develop"))

    def test_a_pr_mode_worker_lands_by_pr_on_base(self):
        self.afk("init", str(self.scratch))

        self.afk("start", "03")

        self.assertEqual(self.worktree_base(), "main")
        self.assertEqual((self.status()["landing"], self.status()["target"]), ("pr", "main"))

    def deny(self):
        """The deny list the worker's claude was launched with."""
        return json.loads((self.project_dir / "workers" / "03" / "settings.json").read_text())["permissions"]["deny"]

    def test_a_local_mode_worker_is_denied_moving_or_checking_out_its_target_as_well_as_pushing_base(self):
        self.afk("init", str(self.scratch), "--land", "local", "--target", "widgets-v2")

        self.afk("start", "03")

        for rule in [
            "Bash(git push * main)",
            "Bash(git push * widgets-v2)",
            "Bash(git push * HEAD:widgets-v2)",
            "Bash(git push * *:widgets-v2)",
            "Bash(git checkout widgets-v2)",
            "Bash(git checkout * widgets-v2)",
            "Bash(git switch widgets-v2)",
            "Bash(git switch * widgets-v2)",
        ]:
            self.assertIn(rule, self.deny())

    def test_a_local_mode_worker_is_denied_moving_its_target_but_not_a_branch_whose_name_holds_it(self):
        self.afk("init", str(self.scratch), "--land", "local")

        self.afk("start", "03")

        denied = lambda command: any(fnmatch.fnmatchcase(f"Bash({command})", rule) for rule in self.deny())
        self.assertTrue(denied("git update-ref refs/heads/main 3f2a9c1"))
        self.assertTrue(denied("git update-ref -d refs/heads/main"))
        self.assertFalse(denied("git update-ref refs/heads/afk/domain-03 3f2a9c1"))
        self.assertFalse(denied("git update-ref refs/heads/maintenance 3f2a9c1"))

    def test_a_worker_is_denied_dropping_any_ticket_which_is_the_humans_call(self):
        self.afk("init", str(self.scratch), "--land", "local")

        self.afk("start", "03")

        denied = lambda command: any(fnmatch.fnmatchcase(f"Bash({command})", rule) for rule in self.deny())
        self.assertTrue(denied("afk drop 03 --discard"))
        self.assertTrue(denied(f"{shlex.quote(afk.AFK_BIN)} drop 04 --discard"))
        self.assertFalse(denied("afk report done 'Dropped the unused helper'"))

    def test_a_pr_mode_worker_is_denied_nothing_about_its_target_beyond_pushing_base(self):
        self.afk("init", str(self.scratch))

        self.afk("start", "03")

        self.assertEqual([r for r in self.deny() if "main" in r],
                         ["Bash(git push * main)", "Bash(git push * HEAD:main)", "Bash(git push * *:main)"])

    def test_an_unknown_landing_mode_in_the_project_config_fails_before_creating_anything(self):
        self.afk("init", str(self.scratch))
        with open(self.project_dir / "config.toml", "a") as config:
            config.write('\n[landing]\nmode = "Local"\n')
        out = io.StringIO()

        code = afk.main(["start", "03"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("unknown landing mode 'Local'", out.getvalue())
        self.assertFalse((self.project_dir / "workers" / "03").exists())


TASK_TYPES = """
default_type = "backend"

[task_types.backend]
agent = "claude"
model = "opus"
effort = "high"
skill = "/tdd"

[task_types.frontend]
agent = "claude"
model = "sonnet"
effort = "medium"
prompt = "docs/agents/prompts/frontend.md"
"""


class TaskTypeTest(AfkTestCase):
    def setUp(self):
        super().setUp()
        self.ticket("03-ui", "Widget UI", type="frontend")
        self.ticket("05-export", "Widget export")
        self.afk("init", str(self.scratch))
        self.repo_config(TASK_TYPES)
        prompts = self.repo / "docs" / "agents" / "prompts"
        prompts.mkdir()
        (prompts / "frontend.md").write_text("Match the Figma design linked in {{ticket_path}}.\n")

    def claude(self):
        [send] = self.run_fake.find("tmux", "send-keys")
        launch = shlex.split(send[4])
        return launch[launch.index("claude") :]

    def flag(self, claude, name):
        return claude[claude.index(name) + 1]

    def test_tickets_type_selects_model_effort_and_prompt_template(self):
        self.afk("start", "03")

        claude = self.claude()
        self.assertEqual(self.flag(claude, "--model"), "sonnet")
        self.assertEqual(self.flag(claude, "--effort"), "medium")
        prompt = claude[-1]
        self.assertTrue(prompt.startswith("AFK phase: implement"), prompt)
        self.assertIn(f"Match the Figma design linked in {self.scratch / 'issues' / '03-ui.md'}.", prompt)

    def test_untyped_ticket_uses_the_default_type_and_invokes_its_skill(self):
        self.afk("start", "05")

        claude = self.claude()
        self.assertEqual(self.flag(claude, "--model"), "opus")
        self.assertEqual(self.flag(claude, "--effort"), "high")
        self.assertTrue(claude[-1].startswith("/tdd AFK phase: implement"), claude[-1])

    def test_type_flag_overrides_the_tickets_type(self):
        self.afk("start", "03", "--type", "backend")

        self.assertEqual(self.flag(self.claude(), "--model"), "opus")

    def test_local_override_file_replaces_just_the_fields_it_sets(self):
        self.repo_config('[task_types.frontend]\nmodel = "opus"', name="afk.local.md")

        self.afk("start", "03")

        claude = self.claude()
        self.assertEqual(self.flag(claude, "--model"), "opus")
        self.assertEqual(self.flag(claude, "--effort"), "medium")

    def test_project_config_overrides_win_over_the_local_override_file(self):
        self.repo_config('[task_types.frontend]\nmodel = "opus"\neffort = "low"', name="afk.local.md")
        with open(self.project_dir / "config.toml", "a") as config:
            config.write('\n[overrides.task_types.frontend]\nmodel = "haiku"\n')

        self.afk("start", "03")

        claude = self.claude()
        self.assertEqual(self.flag(claude, "--model"), "haiku")
        self.assertEqual(self.flag(claude, "--effort"), "low")

    def start_fails(self, *argv):
        out = io.StringIO()
        code = afk.main(["start", *argv], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out)
        self.assertNotEqual(code, 0)
        self.assertEqual(self.run_fake.find("git"), [])
        self.assertEqual(self.run_fake.find("tmux", "new-window"), [])
        self.assertFalse((self.project_dir / "workers" / argv[0]).exists())
        return out.getvalue()

    def test_unknown_task_type_fails_before_creating_anything(self):
        out = self.start_fails("03", "--type", "infra")

        self.assertIn("infra", out)
        self.assertIn("backend, frontend", out)

    def test_malformed_options_fail_with_usage_before_creating_anything(self):
        for options in (["--type"], ["--typ", "backend"], ["--type", "backend", "extra"]):
            with self.subTest(options=options):
                self.assertIn("usage: afk start <ticket> [--type <type>]", self.start_fails("03", *options))

    def test_unsupported_agent_fails_before_creating_anything(self):
        self.repo_config('[task_types.frontend]\nagent = "grok"', name="afk.local.md")

        out = self.start_fails("03")

        self.assertIn("grok", out)

    def test_without_task_types_claude_launches_with_its_own_defaults(self):
        self.repo_config("")

        self.afk("start", "05")

        claude = self.claude()
        self.assertNotIn("--model", claude)
        self.assertNotIn("--effort", claude)
        self.assertTrue(claude[-1].startswith("AFK phase: implement"))


PROSE = "# AFK\n\n## Pre-PR skill\n\n`/pre-pr`\n\n## Dev server\n\n`npm run dev`\n\n## Verification recipes\n\nCurl it.\n"


class CheckTest(AfkTestCase):
    """`afk check` validates a repo's config outside any project, as /afk-setup does."""

    def setUp(self):
        super().setUp()
        self.run_fake.responses[("git", "rev-parse", "--show-toplevel")] = f"{self.repo}\n"

    def check(self, *argv):
        out = io.StringIO()
        code = afk.main(["check", *argv], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out)
        return code, out.getvalue()

    def test_a_complete_config_is_valid(self):
        (self.repo / ".env").write_text("X=1\n")
        self.repo_config('copy = [".env"]\nbootstrap = ["npm ci"]\nverify_concurrency = 1\n' + TASK_TYPES, body=PROSE)
        prompts = self.repo / "docs" / "agents" / "prompts"
        prompts.mkdir()
        (prompts / "frontend.md").write_text("Design: see ticket.\n")

        code, out = self.check()

        self.assertEqual(code, 0, out)
        self.assertEqual(out, f"{self.repo / 'docs' / 'agents' / 'afk.md'} is valid\n")

    def test_checks_the_repo_it_is_given_or_else_the_current_checkout(self):
        self.repo_config('base = "main"', body=PROSE)

        self.assertEqual(self.check(str(self.repo))[0], 0)
        self.assertEqual(self.check()[0], 0)
        self.assertEqual(self.run_fake.find("git", "-C", str(self.repo), "rev-parse", "--verify")[0][-1], "main^{commit}")

    def test_a_missing_config_file_is_an_error(self):
        code, out = self.check()

        self.assertEqual(code, 1)
        self.assertIn("afk.md not found", out)

    def test_a_file_without_frontmatter_or_with_bad_toml_is_an_error(self):
        path = self.repo / "docs" / "agents" / "afk.md"
        path.parent.mkdir(parents=True)
        path.write_text("# AFK\n")
        self.assertIn("no TOML frontmatter", self.check()[1])

        self.repo_config('base = main')
        code, out = self.check()
        self.assertEqual(code, 1)
        self.assertIn("afk.md: invalid TOML", out)

    def test_every_schema_problem_is_reported_at_once(self):
        self.repo_config(
            'bootstrap = "npm ci"\nport_base = "4000"\nverify_concurrency = 0\ncopy = [".env"]\n'
            'default_type = "infra"\nsurprise = 1\n'
            '[task_types.backend]\nagent = "grok"\nmodle = "opus"\nprompt = "missing.md"\n',
            body=PROSE,
        )

        code, out = self.check()

        self.assertEqual(code, 1)
        for problem in (
            "'bootstrap' must be a list of strings",
            "'port_base' must be an integer",
            "'verify_concurrency' must be an integer of at least 1",
            "unknown key 'surprise'",
            "default_type 'infra' is not a task type",
            "task_types.backend: unsupported agent 'grok'",
            "task_types.backend: unknown key 'modle'",
            "task_types.backend: prompt template missing.md not found",
            "copy: .env not found",
        ):
            self.assertIn(problem, out)

    def test_the_local_override_file_is_checked_as_merged(self):
        self.repo_config("[task_types.backend]\nmodel = \"opus\"", body=PROSE)
        self.repo_config('[task_types.backend]\nagent = "grok"', name="afk.local.md")

        self.assertIn("unsupported agent 'grok'", self.check()[1])

    def test_a_missing_base_branch_is_an_error(self):
        def no_branch(cmd):
            raise subprocess.CalledProcessError(1, cmd)

        self.run_fake.responses[("git", "-C", str(self.repo), "rev-parse")] = no_branch
        self.repo_config('base = "develop"', body=PROSE)

        code, out = self.check()

        self.assertEqual(code, 1)
        self.assertIn("base branch 'develop' does not exist", out)

    def test_reviewers_must_be_a_list_of_commands(self):
        self.repo_config('reviewers = "coderabbit review"', body=PROSE)

        code, out = self.check()

        self.assertEqual(code, 1)
        self.assertIn("'reviewers' must be a list of strings", out)

    def test_a_reviewer_that_is_not_a_runnable_command_is_an_error(self):
        self.repo_config('reviewers = ["copilot -p \'unbalanced", "", "FOO=1"]', body=PROSE)

        code, out = self.check()

        self.assertEqual(code, 1)
        self.assertIn("reviewer `copilot -p 'unbalanced`: No closing quotation", out)
        self.assertIn("reviewer `` names no command", out)
        self.assertIn("reviewer `FOO=1` names no command", out)

    def test_a_reviewer_whose_binary_is_not_on_path_warns_without_failing(self):
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "coderabbit").write_text("#!/bin/sh\n")
        (bin_dir / "coderabbit").chmod(0o755)
        self.env["PATH"] = str(bin_dir)
        self.repo_config(
            'reviewers = ["coderabbit review --base {{target}} --plain", "copilot -p \'review the diff\'"]', body=PROSE
        )

        code, out = self.check()

        self.assertEqual(code, 0, out)
        self.assertIn("warning: reviewer `copilot -p 'review the diff'`: copilot is not on PATH", out)
        self.assertNotIn("coderabbit is not on PATH", out)

    def test_missing_prose_sections_warn_without_failing(self):
        self.repo_config('base = "main"', body="# AFK\n\n## Dev server\n\n`npm run dev`\n")

        code, out = self.check()

        self.assertEqual(code, 0)
        self.assertIn("warning: afk.md has no '## Pre-PR skill' section", out)
        self.assertIn("warning: afk.md has no '## Verification recipes' section", out)
        self.assertNotIn("Dev server' section", out)


class TrialTest(AfkTestCase):
    """`afk trial` proves the config through the same copy and bootstrap `afk start` uses."""

    def setUp(self):
        super().setUp()
        self.run_fake.responses[("git", "rev-parse", "--show-toplevel")] = f"{self.repo}\n"
        self.worktree = self.state_home / "afk" / ".trial" / "repo"
        (self.repo / ".env").write_text("SECRET=1\n")

        def add_worktree(cmd):
            Path(cmd[-2]).mkdir(parents=True)
            return ""

        self.run_fake.responses[("git", "-C", str(self.repo), "worktree", "add")] = add_worktree

    def trial(self, *argv):
        out = io.StringIO()
        code = afk.main(["trial", *argv], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out)
        return code, out.getvalue()

    def bootstraps(self):
        return [(cmd, cwd) for cmd, cwd in zip(self.run_fake.calls, self.run_fake.cwds) if cmd[-3:-1] == ["sh", "-c"]]

    def test_trial_creates_a_scratch_worktree_from_base_copies_files_and_bootstraps_on_a_trial_slot(self):
        self.repo_config('base = "develop"\nport_base = 5000\ncopy = [".env"]\nbootstrap = ["npm ci", "make db"]', body=PROSE)

        code, out = self.trial()

        self.assertEqual(code, 0, out)
        [add] = self.run_fake.find("git", "-C", str(self.repo), "worktree", "add")
        self.assertEqual(add[-3:], ["afk/trial", str(self.worktree), "develop"])
        self.assertEqual((self.worktree / ".env").read_text(), "SECRET=1\n")
        self.assertEqual(
            self.bootstraps(),
            [
                (["env", "AFK_SLOT=9", "AFK_PORT_BASE=5900", "sh", "-c", "npm ci"], str(self.worktree)),
                (["env", "AFK_SLOT=9", "AFK_PORT_BASE=5900", "sh", "-c", "make db"], str(self.worktree)),
            ],
        )
        self.assertIn("AFK_SLOT=9 AFK_PORT_BASE=5900", out.splitlines()[-1])
        self.assertIn(str(self.worktree), out.splitlines()[-1])

    def test_a_failed_step_is_named_with_its_output_and_the_trial_is_torn_down(self):
        def make_fails(cmd):
            if cmd[-1] == "make db":
                raise subprocess.CalledProcessError(2, cmd, stderr="make: *** No rule to make target 'db'.")
            return ""

        self.run_fake.responses[("env",)] = make_fails
        self.repo_config('bootstrap = ["npm ci", "make db"]', body=PROSE)

        code, out = self.trial()

        self.assertEqual(code, 1)
        self.assertIn("FAIL bootstrap `make db` (exit 2): make: *** No rule to make target 'db'.", out)
        self.assertFalse(self.worktree.exists())
        self.assertEqual(self.run_fake.find("git", "-C", str(self.repo), "branch", "-D")[-1][-1], "afk/trial")

    def test_an_invalid_config_is_reported_before_anything_is_created(self):
        self.repo_config('bootstrap = "npm ci"', body=PROSE)

        code, out = self.trial()

        self.assertEqual(code, 1)
        self.assertIn("'bootstrap' must be a list of strings", out)
        self.assertEqual(self.run_fake.find("git", "-C", str(self.repo), "worktree", "add"), [])

    def test_teardown_removes_the_worktree_and_branch(self):
        self.repo_config('base = "main"', body=PROSE)
        self.trial()

        code, out = self.trial("--teardown")

        self.assertEqual(code, 0, out)
        self.assertFalse(self.worktree.exists())
        self.assertIn(["git", "-C", str(self.repo), "worktree", "remove", "--force", str(self.worktree)], self.run_fake.calls)
        self.assertEqual(self.run_fake.find("git", "-C", str(self.repo), "branch", "-D")[-1][-1], "afk/trial")

    def test_a_leftover_trial_is_cleared_before_a_new_one(self):
        self.repo_config('base = "main"', body=PROSE)
        self.trial()

        code, out = self.trial()

        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.run_fake.find("git", "-C", str(self.repo), "worktree", "add")), 2)


class SlotTest(AfkTestCase):
    def setUp(self):
        super().setUp()
        for name, title in [("03-ui", "Widget UI"), ("05-export", "Widget export"), ("07-import", "Widget import")]:
            self.ticket(name, title)
        self.afk("init", str(self.scratch))
        self.repo_config('port_base = 5000\nbootstrap = ["make db"]')
        panes = iter(["%5", "%6", "%7", "%8"])
        self.run_fake.responses[("tmux", "new-window")] = lambda cmd: next(panes) + "\n"

    def slot_env(self, cmd):
        return [arg for arg in cmd if arg.startswith(("AFK_SLOT=", "AFK_PORT_BASE="))]

    def launches(self):
        return [self.slot_env(shlex.split(c[4])) for c in self.run_fake.find("tmux", "send-keys") if len(c) > 5]

    def test_each_worker_gets_its_own_slot_and_port_base(self):
        self.afk("start", "03")
        self.afk("start", "05")

        self.assertEqual(
            self.launches(),
            [["AFK_SLOT=1", "AFK_PORT_BASE=5100"], ["AFK_SLOT=2", "AFK_PORT_BASE=5200"]],
        )

    def test_bootstrap_sees_the_workers_slot(self):
        self.afk("start", "03")

        [bootstrap] = [c for c in self.run_fake.calls if c[-1] == "make db"]
        self.assertEqual(self.slot_env(bootstrap), ["AFK_SLOT=1", "AFK_PORT_BASE=5100"])

    def test_slot_of_a_rolled_back_start_is_reused(self):
        self.afk("start", "03")
        self.run_fake.responses[("tmux", "split-window")] = lambda cmd: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, cmd, stderr="no space for new pane")
        )
        afk.main(["start", "05"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=io.StringIO())
        del self.run_fake.responses[("tmux", "split-window")]

        self.afk("start", "07")

        self.assertEqual(self.launches()[-1], ["AFK_SLOT=2", "AFK_PORT_BASE=5200"])

    def test_port_base_defaults_to_4000(self):
        self.repo_config("")

        self.afk("start", "03")

        self.assertEqual(self.launches(), [["AFK_SLOT=1", "AFK_PORT_BASE=4100"]])


class VerifyQueueTest(AfkTestCase):
    PANES = {"03": "%5", "05": "%6", "07": "%7"}

    def setUp(self):
        super().setUp()
        for name, title in [("03-ui", "Widget UI"), ("05-export", "Widget export"), ("07-import", "Widget import")]:
            self.ticket(name, title)
        self.afk("init", str(self.scratch))
        self.repo_config("verify_concurrency = 1")
        panes = iter(self.PANES.values())
        self.run_fake.responses[("tmux", "new-window")] = lambda cmd: next(panes) + "\n"
        for ticket in self.PANES:
            self.afk("start", ticket)

    def finish(self, ticket):
        env = {"AFK_PROJECT": "widgets", "AFK_TICKET": ticket}
        self.afk("report", "done", "Done", env=env)
        self.afk("hook", "stop", stdin=json.dumps({"hook_event_name": "Stop"}), env=env)
        self.now += 60

    def state(self, ticket):
        status = json.loads((self.project_dir / "workers" / ticket / "status.json").read_text())
        return status["phase"], status["state"]

    def verify_prompted(self, ticket):
        pane = self.PANES[ticket]
        pastes = [c for c in self.run_fake.find("tmux", "paste-buffer") if c[c.index("-t") + 1] == pane]
        buffers = [i for c, i in zip(self.run_fake.calls, self.run_fake.inputs) if c[:2] == ["tmux", "load-buffer"]]
        return len(pastes) > 0 and any(b.startswith(f"AFK phase: verify — ticket {ticket}") for b in buffers)

    def test_second_worker_is_queued_while_another_verifies(self):
        self.finish("03")
        self.afk("tick")
        self.finish("05")

        self.afk("tick")

        self.assertEqual(self.state("03"), ("verify", "working"))
        self.assertEqual(self.state("05"), ("implement", "queued"))
        self.assertFalse(self.verify_prompted("05"))

    def test_queued_worker_starts_verifying_once_the_slot_frees(self):
        self.finish("03")
        self.afk("tick")
        self.finish("05")
        self.afk("tick")

        self.finish("03")
        self.afk("tick")
        self.afk("tick")

        self.assertEqual(self.state("03"), ("prepr", "working"))
        self.assertEqual(self.state("05"), ("verify", "working"))
        self.assertTrue(self.verify_prompted("05"))

    def test_workers_finishing_in_the_same_tick_verify_one_at_a_time(self):
        self.finish("03")
        self.finish("05")

        self.afk("tick")

        self.assertEqual([self.state("03")[1], self.state("05")[1]], ["working", "queued"])

    def test_queue_is_first_come_first_served(self):
        self.finish("03")
        self.afk("tick")
        self.finish("07")
        self.afk("tick")
        self.finish("05")
        self.afk("tick")

        self.finish("03")
        self.afk("tick")
        self.afk("tick")

        self.assertEqual(self.state("07"), ("verify", "working"))
        self.assertEqual(self.state("05"), ("implement", "queued"))

    def test_queued_worker_whose_agent_exited_needs_attention_without_skipping_verify(self):
        self.finish("03")
        self.afk("tick")
        self.finish("05")
        self.afk("tick")
        self.run_fake.responses[("tmux", "display-message", "-p", "-t")] = lambda cmd: "claude\n" if cmd[4] == "%5" else "bash\n"

        self.finish("03")
        self.afk("tick")

        self.assertEqual(self.state("05"), ("implement", "attention"))
        self.assertFalse(self.verify_prompted("05"))

    def test_overlapping_ticks_still_admit_one_verifier(self):
        self.finish("05")
        self.finish("07")
        results = []

        def second_tick():
            out = io.StringIO()
            try:
                results.append(afk.main(["tick"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out, clock=lambda: self.now))
            except Exception as error:
                results.append(error)

        other = threading.Thread(target=second_tick)

        def another_tick_starts_mid_paste(cmd):
            if cmd[cmd.index("-t") + 1] == "%6" and not other.is_alive() and not results:
                other.start()
                other.join(timeout=0.2)  # without a tick-wide lock it snapshots the state before this tick writes it
            return ""

        self.run_fake.responses[("tmux", "paste-buffer")] = another_tick_starts_mid_paste
        self.afk("tick")
        other.join()

        self.assertEqual(results, [0])
        self.assertEqual([self.state("05"), self.state("07")], [("verify", "working"), ("implement", "queued")])
        self.assertFalse(self.verify_prompted("07"))

    def test_without_verify_concurrency_workers_verify_in_parallel(self):
        self.repo_config("")
        self.finish("03")
        self.finish("05")

        self.afk("tick")

        self.assertEqual([self.state("03"), self.state("05")], [("verify", "working")] * 2)


class WorkerStatusTest(AfkTestCase):
    def setUp(self):
        super().setUp()
        self.ticket("03-ui", "Widget UI")
        self.ticket("05-export", "Widget export")
        self.afk("init", str(self.scratch))
        self.afk("start", "03")
        self.afk("start", "05")

    def worker_env(self, ticket):
        return {"AFK_PROJECT": "widgets", "AFK_TICKET": ticket}

    def status_rows(self):
        return [line.split(None, 3) for line in self.afk("status").splitlines()[1:]]

    def test_status_shows_each_started_worker_implementing(self):
        self.assertEqual(
            self.status_rows(),
            [["03", "implement", "working"], ["05", "implement", "working"]],
        )

    def test_report_from_worker_shows_in_status(self):
        self.afk("report", "question", "Which database?", env=self.worker_env("03"))
        self.afk("report", "done", "Export works", env=self.worker_env("05"))

        self.assertEqual(
            self.status_rows(),
            [["03", "implement", "question", "Which database?"], ["05", "implement", "done", "Export works"]],
        )

    def test_report_rejects_unknown_state(self):
        out = io.StringIO()
        code = afk.main(
            ["report", "finished", "hi"],
            run=self.run_fake,
            env={**self.env, **self.worker_env("03")},
            stdin=io.StringIO(),
            stdout=out,
        )
        self.assertNotEqual(code, 0)
        self.assertEqual(self.status_rows()[0], ["03", "implement", "working"])

    def status_file(self, ticket):
        return json.loads((self.project_dir / "workers" / ticket / "status.json").read_text())

    def test_hooks_record_session_id_transcript_and_event_in_status_file(self):
        for event, name in [("session-start", "SessionStart"), ("notification", "Notification"), ("stop", "Stop")]:
            payload = {
                "session_id": "sess-123",
                "transcript_path": "/home/u/.claude/projects/x/sess-123.jsonl",
                "hook_event_name": name,
            }
            self.afk("hook", event, stdin=json.dumps(payload), env=self.worker_env("03"))

            status = self.status_file("03")
            self.assertEqual(status["session_id"], "sess-123")
            self.assertEqual(status["transcript_path"], "/home/u/.claude/projects/x/sess-123.jsonl")
            self.assertEqual(status["last_event"], name)
        self.assertNotIn("session_id", self.status_file("05"))

    def test_hook_keeps_reported_state(self):
        self.afk("report", "done", "Finished", env=self.worker_env("03"))
        self.afk("hook", "stop", stdin=json.dumps({"session_id": "s", "transcript_path": "/t"}), env=self.worker_env("03"))

        self.assertEqual(self.status_rows()[0], ["03", "implement", "done", "Finished"])


class WorkerTestCase(AfkTestCase):
    """A project with worker 03 started, init'd with INIT's extra arguments, and helpers to act as it."""

    INIT = ()

    def setUp(self):
        super().setUp()
        self.ticket("03-ui", "Widget UI")
        self.ticket("09-docs", "Widget docs", status="claimed")  # someone else's, so the project never finishes
        self.afk("init", str(self.scratch), *self.INIT)
        self.run_fake.responses[("tmux", "split-window")] = "%6\n"  # the worker's shell pane
        self.afk("start", "03")
        self.calls_before = len(self.run_fake.calls)

    def worker_env(self, ticket="03"):
        return {"AFK_PROJECT": "widgets", "AFK_TICKET": ticket}

    def report(self, state, message="", ticket="03"):
        self.afk("report", state, message, env=self.worker_env(ticket))

    def stop(self, ticket="03"):
        payload = {"session_id": "s", "transcript_path": str(self.transcript(ticket)), "hook_event_name": "Stop"}
        self.afk("hook", "stop", stdin=json.dumps(payload), env=self.worker_env(ticket))

    def resume(self, ticket="03"):
        """The stopped worker's session takes up a tool again, as when a background task it waited on lands."""
        payload = {"session_id": "s", "transcript_path": str(self.transcript(ticket)), "hook_event_name": "PreToolUse"}
        self.afk("hook", "activity", stdin=json.dumps(payload), env=self.worker_env(ticket))

    def prompt(self, text, ticket="03"):
        """Text submitted in the worker's pane, typed by the human or pasted by the watcher."""
        payload = {"session_id": "s", "transcript_path": str(self.transcript(ticket)), "hook_event_name": "UserPromptSubmit",
                   "prompt": text}
        self.afk("hook", "prompt", stdin=json.dumps(payload), env=self.worker_env(ticket))

    def transcript(self, ticket="03"):
        return self.tmp / f"{ticket}.jsonl"

    def active(self, ticket="03"):
        """The worker's session writes to its transcript now, as a busy agent does."""
        path = self.transcript(ticket)
        path.touch()
        os.utime(path, (self.now, self.now))
        payload = {"session_id": "s", "transcript_path": str(path), "hook_event_name": "SessionStart"}
        self.afk("hook", "session-start", stdin=json.dumps(payload), env=self.worker_env(ticket))

    def prompts_sent(self, pane="%5"):
        """Prompts pasted into a pane and submitted since setUp, in order."""
        calls = list(zip(self.run_fake.calls, self.run_fake.inputs))[self.calls_before :]
        prompts, buffers = [], {}
        for cmd, stdin in calls:
            if cmd[:2] == ["tmux", "load-buffer"]:
                buffers[cmd[cmd.index("-b") + 1]] = stdin
            elif cmd[:2] == ["tmux", "paste-buffer"] and cmd[cmd.index("-t") + 1] == pane:
                self.assertIn("-p", cmd)  # bracketed paste, so newlines don't submit early
                prompts.append(buffers[cmd[cmd.index("-b") + 1]])
            elif cmd[:2] == ["tmux", "send-keys"] and cmd[cmd.index("-t") + 1] == pane and cmd[-1] != "Escape":  # an interrupt
                self.assertEqual(cmd[-1], "Enter")
        return prompts

    def phase(self, ticket="03"):
        return json.loads((self.project_dir / "workers" / ticket / "status.json").read_text())["phase"]

    def status(self, ticket="03"):
        return json.loads((self.project_dir / "workers" / ticket / "status.json").read_text())

    def desktop_notifications(self):
        return [" ".join(c[1:]) for c in self.run_fake.calls[self.calls_before :] if c[0] == "notify-send"]

    def finish_phase(self, message="Done"):
        self.report("done", message)
        self.stop()
        self.afk("tick")

    def limit(self, **limits):
        with open(self.project_dir / "config.toml", "a") as config:
            config.write("\n[limits]\n" + "".join(f"{k} = {v}\n" for k, v in limits.items()))

    def interrupts(self, pane="%5"):
        return [c for c in self.run_fake.find("tmux", "send-keys")[1:] if c[c.index("-t") + 1] == pane and c[-1] == "Escape"]


class WatchTest(WorkerTestCase):
    def test_done_report_then_stop_advances_to_verify_and_sends_its_prompt(self):
        self.report("done", "Implemented")
        self.stop()

        self.afk("tick")

        self.assertEqual(self.phase(), "verify")
        [prompt] = self.prompts_sent()
        self.assertTrue(prompt.startswith("AFK phase: verify"), prompt)
        self.assertIn(str(self.scratch / "issues" / "03-ui.md"), prompt)

    def test_done_report_while_worker_is_still_busy_sends_nothing(self):
        self.report("done", "Implemented")

        self.afk("tick")

        self.assertEqual(self.phase(), "implement")
        self.assertEqual(self.prompts_sent(), [])

    def test_stop_left_over_from_before_the_report_does_not_count_as_idle(self):
        self.stop()
        self.report("done", "Implemented")

        self.afk("tick")

        self.assertEqual(self.prompts_sent(), [])

    def test_each_done_phase_advances_to_the_next_until_review(self):
        for _ in range(4):
            self.finish_phase()

        self.assertEqual(self.phase(), "review")
        self.assertEqual(self.status()["state"], "review")
        self.assertEqual(
            [p.splitlines()[0].split(" — ")[0] for p in self.prompts_sent()],
            ["AFK phase: verify", "AFK phase: prepr", "AFK phase: pr", "AFK phase: review"],
        )

    def test_question_and_blocked_reports_notify_once_without_advancing(self):
        for state, message in [("question", "Which database?"), ("blocked", "CI is down")]:
            with self.subTest(state):
                self.report(state, message)
                self.stop()

                self.afk("tick")
                self.afk("tick")

                self.assertEqual(self.phase(), "implement")
                self.assertEqual(self.prompts_sent(), [])
                [notification] = [n for n in self.desktop_notifications() if message in n]
                self.assertIn("03", notification)
                self.assertIn(state, notification)

    def test_same_question_asked_again_after_an_answer_notifies_again(self):
        self.report("question", "Which database?")
        self.stop()
        self.afk("tick")
        self.report("question", "Which database?")
        self.stop()

        self.afk("tick")

        self.assertEqual(len(self.desktop_notifications()), 2)

    def reach_review(self, pr="https://github.com/acme/widgets/pull/42"):
        for message in ["Implemented", "Verified", "Checks pass", pr]:
            self.finish_phase(message)

    def test_reaching_review_notifies_with_the_pr(self):
        self.reach_review()

        [notification] = self.desktop_notifications()
        self.assertIn("03", notification)
        self.assertIn("https://github.com/acme/widgets/pull/42", notification)

    def test_a_done_report_without_a_url_reaches_review_with_the_url_found_by_branch(self):
        self.reach_review(pr="Checks green, no conflicts")

        [notification] = self.desktop_notifications()
        self.assertIn("https://github.com/acme/widgets/pull/42", notification)
        self.assertEqual(self.status()["pr"], "https://github.com/acme/widgets/pull/42")

    def test_done_after_review_feedback_stays_in_review_and_notifies(self):
        self.reach_review()
        prompts = len(self.prompts_sent())

        self.finish_phase("Renamed the endpoint as asked")
        self.afk("tick")

        self.assertEqual((self.phase(), self.status()["state"]), ("review", "review"))
        self.assertEqual(len(self.prompts_sent()), prompts)
        self.assertEqual(len(self.desktop_notifications()), 2)
        self.assertIn("Renamed the endpoint as asked", self.desktop_notifications()[-1])

    def stall(self, ticket="03"):
        """The worker stops without reporting, and nothing wakes it again for longer than the idle limit."""
        self.stop(ticket)
        self.now += (afk.LIMITS["idle_minutes"] + 1) * 60

    def test_stop_without_a_report_that_stays_quiet_past_the_idle_limit_needs_attention_and_notifies_once(self):
        self.stall()

        self.afk("tick")
        self.afk("tick")

        self.assertEqual((self.phase(), self.status()["state"]), ("implement", "attention"))
        [notification] = self.desktop_notifications()
        self.assertIn("03", notification)
        self.assertIn("attention", notification)

    def test_stop_without_a_report_after_a_new_phase_prompt_needs_attention(self):
        self.finish_phase()

        self.stall()
        self.afk("tick")

        self.assertEqual((self.phase(), self.status()["state"]), ("verify", "attention"))

    def test_a_worker_that_stops_to_wait_on_a_background_task_keeps_working(self):
        # prepr ends its turn while its code-review subagent runs, and resumes when the review lands.
        self.finish_phase("Implemented")
        self.finish_phase("Verified")
        notifications = len(self.desktop_notifications())
        self.stop()
        self.afk("tick")
        self.now += (afk.LIMITS["idle_minutes"] - 1) * 60
        self.active()
        self.now += (afk.LIMITS["idle_minutes"] - 1) * 60

        self.afk("tick")

        self.assertEqual((self.phase(), self.status()["state"]), ("prepr", "working"))
        self.assertEqual(len(self.desktop_notifications()), notifications)
        self.assertEqual(self.interrupts(), [])

    def test_standing_by_in_review_does_not_need_attention(self):
        self.reach_review()

        self.stop()
        self.afk("tick")

        self.assertEqual(self.status()["state"], "review")

    def test_worker_that_needed_attention_can_report_done_and_move_on(self):
        self.stop()
        self.afk("tick")

        self.finish_phase()

        self.assertEqual((self.phase(), self.status()["state"]), ("verify", "working"))

    def test_tick_renders_dashboard_of_each_tickets_phase_state_time_in_phase_and_pr(self):
        self.ticket("05-export", "Widget export")
        self.afk("start", "05")
        self.now += 60
        self.reach_review()
        self.now += 3600 + 300
        self.active("05")

        dashboard = self.afk("tick").splitlines()

        self.assertEqual(dashboard[0].split(), ["TICKET", "PHASE", "STATE", "ELAPSED", "PR"])
        self.assertEqual(
            [line.split() for line in dashboard[1:]],
            [
                ["03", "review", "review", "1h05m", "https://github.com/acme/widgets/pull/42"],
                ["05", "implement", "working", "1h06m", "-"],
            ],
        )

    def test_dashboard_shows_short_elapsed_times_in_minutes_and_seconds(self):
        self.now += 45
        self.assertEqual(self.afk("tick").splitlines()[1].split()[3], "45s")
        self.now += 12 * 60
        self.assertEqual(self.afk("tick").splitlines()[1].split()[3], "12m")

    def window(self, pane="%5"):
        """The worker window's last applied name and @afk_state since setUp."""
        name = state = None
        for cmd in self.run_fake.calls[self.calls_before :]:
            if cmd[:2] == ["tmux", "rename-window"] and cmd[cmd.index("-t") + 1] == pane:
                name = cmd[-1]
            if cmd[:2] == ["tmux", "set-option"] and "@afk_state" in cmd and cmd[cmd.index("-t") + 1] == pane:
                self.assertIn("-w", cmd)
                state = cmd[-1]
        return name, state

    def test_window_name_and_state_option_follow_the_worker(self):
        self.report("question", "Which database?")
        self.stop()
        self.afk("tick")
        self.assertEqual(self.window(), ("03-widget-ui?", "question"))

        self.finish_phase()
        self.assertEqual(self.window(), ("03-widget-ui", "working"))

        self.stall()
        self.afk("tick")
        self.assertEqual(self.window(), ("03-widget-ui!", "attention"))

        for message in ["Verified", "Checks pass", "https://github.com/acme/widgets/pull/42"]:
            self.finish_phase(message)
        self.assertEqual(self.window(), ("03-widget-ui✓", "review"))

    def test_window_is_only_updated_when_the_state_changes(self):
        self.report("blocked", "CI is down")
        self.stop()
        self.afk("tick")
        renames = len(self.run_fake.find("tmux", "rename-window"))

        self.afk("tick")

        self.assertEqual(len(self.run_fake.find("tmux", "rename-window")), renames)

    def test_notify_rings_the_bell_and_shows_a_tmux_message_as_well_as_a_desktop_notification(self):
        self.report("question", "Which database?")
        self.stop()

        output = self.afk("tick")

        self.assertIn("\a", output)
        [message] = [c for c in self.run_fake.find("tmux", "display-message") if "Which database?" in c[-1]]
        self.assertEqual(message[message.index("-t") + 1], "work:")
        self.assertEqual(len(self.desktop_notifications()), 1)

    def test_quiet_ticks_do_not_ring_the_bell(self):
        self.assertNotIn("\a", self.afk("tick"))

    def test_a_failing_notification_backend_does_not_stop_the_watcher(self):
        def missing(cmd):
            raise FileNotFoundError(2, "No such file or directory", "notify-send")

        self.run_fake.responses[("notify-send",)] = missing
        self.report("question", "Which database?")
        self.stop()

        self.afk("tick")

        self.assertTrue([c for c in self.run_fake.find("tmux", "display-message") if "Which database?" in c[-1]])
        self.afk("tick")
        self.assertEqual(len(self.desktop_notifications()), 1)

    def test_a_prompt_that_fails_to_send_does_not_resend_other_workers_prompts(self):
        self.ticket("05-export", "Widget export")
        self.run_fake.responses[("tmux", "new-window")] = "%7\n"
        self.afk("start", "05")
        self.calls_before = len(self.run_fake.calls)
        for ticket in ("03", "05"):
            self.report("done", "Implemented", ticket=ticket)
            self.stop(ticket=ticket)

        def pane_gone(cmd):
            if "%7" in cmd:
                raise subprocess.CalledProcessError(1, cmd, stderr="can't find pane: %7")
            return ""

        self.run_fake.responses[("tmux", "paste-buffer")] = pane_gone
        out = io.StringIO()
        afk.main(["tick"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out, clock=lambda: self.now)
        del self.run_fake.responses[("tmux", "paste-buffer")]

        self.afk("tick")

        self.assertEqual(len(self.prompts_sent("%5")), 1)
        self.assertEqual(len(self.prompts_sent("%7")), 2)  # the failed paste, then its retry
        self.assertEqual((self.phase("03"), self.phase("05")), ("verify", "verify"))

    def test_a_worker_whose_pane_is_gone_does_not_hold_up_the_others(self):
        self.ticket("05-export", "Widget export")
        self.run_fake.responses[("tmux", "new-window")] = "%7\n"
        self.afk("start", "05")
        for ticket in ("03", "05"):
            self.report("done", "Implemented", ticket=ticket)
            self.stop(ticket=ticket)

        def pane_gone(cmd):
            if "%5" in cmd:
                raise subprocess.CalledProcessError(1, cmd, stderr="can't find pane: %5")
            return ""

        self.run_fake.responses[("tmux", "paste-buffer")] = pane_gone
        out = io.StringIO()
        code = afk.main(["tick"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out, clock=lambda: self.now)

        self.assertNotEqual(code, 0)
        self.assertIn("can't find pane: %5", out.getvalue())
        self.assertEqual((self.phase("03"), self.phase("05")), ("implement", "verify"))

    def test_a_report_arriving_while_the_tick_prompts_that_worker_is_not_lost(self):
        self.report("done", "Implemented")
        self.stop()
        reporter = threading.Thread(target=self.report, args=("question", "Which database?"))

        def worker_reports_mid_paste(cmd):
            reporter.start()
            reporter.join(timeout=0.2)  # without a lock it lands now, and the tick's write would clobber it
            return ""

        self.run_fake.responses[("tmux", "paste-buffer")] = worker_reports_mid_paste
        self.afk("tick")
        reporter.join()

        self.assertEqual((self.phase(), self.status()["state"]), ("verify", "question"))

    def test_a_failing_window_update_does_not_resend_the_phase_prompt(self):
        def rename_fails(cmd):
            raise subprocess.CalledProcessError(1, cmd, stderr="server busy")

        self.run_fake.responses[("tmux", "rename-window")] = rename_fails
        self.report("done", "Implemented")
        self.stop()

        self.afk("tick")
        self.afk("tick")

        self.assertEqual(self.phase(), "verify")
        self.assertEqual(len(self.prompts_sent()), 1)

    def test_next_phase_is_not_pasted_into_a_pane_where_claude_has_exited(self):
        self.run_fake.responses[("tmux", "display-message", "-p", "-t")] = "bash\n"
        self.report("done", "Implemented")
        self.stop()

        self.afk("tick")

        self.assertEqual(self.prompts_sent(), [])
        self.assertEqual((self.phase(), self.status()["state"]), ("implement", "attention"))
        [notification] = self.desktop_notifications()
        self.assertIn("03", notification)
        self.assertIn("claude", notification)

    def test_claude_named_by_its_version_still_counts_as_running(self):
        # Claude Code sets its process title to its version, so tmux reports e.g. "2.1.310" rather than "claude".
        self.run_fake.responses[("tmux", "display-message", "-p", "-t")] = "2.1.310\n"
        self.report("done", "Implemented")
        self.stop()

        self.afk("tick")

        self.assertEqual(self.phase(), "verify")
        self.assertEqual(len(self.prompts_sent()), 1)


    def test_a_worker_over_the_configured_phase_limit_is_interrupted_and_stuck(self):
        self.limit(phase_minutes=5)
        self.now += 6 * 60

        dashboard = self.afk("tick").splitlines()

        self.assertEqual(len(self.interrupts()), 1)
        self.assertEqual(self.status()["state"], "stuck")
        self.assertEqual(dashboard[1].split()[:3], ["03", "implement", "stuck"])
        [notification] = self.desktop_notifications()
        self.assertIn("stuck", notification)
        self.assertEqual(self.window(), ("03-widget-ui!", "stuck"))

    def test_the_default_phase_limit_applies_without_config(self):
        self.now += 89 * 60
        self.active()
        self.afk("tick")
        self.assertEqual(self.status()["state"], "working")

        self.now += 2 * 60
        self.active()
        self.afk("tick")
        self.afk("tick")

        self.assertEqual(self.status()["state"], "stuck")
        self.assertEqual(len(self.interrupts()), 1)

    def test_a_stuck_worker_that_recovers_and_reports_done_advances_without_a_second_interrupt(self):
        self.limit(phase_minutes=5)
        self.now += 6 * 60
        self.active()
        self.afk("tick")
        self.assertEqual(self.status()["state"], "stuck")

        self.report("done", "Implemented after a nudge")
        self.afk("tick")
        self.stop()
        self.afk("tick")

        self.assertEqual((self.phase(), self.status()["state"]), ("verify", "working"))
        self.assertEqual(len(self.interrupts()), 1)

    def answer(self, ticket="03"):
        """The human types into the worker's pane."""
        payload = {"session_id": "s", "transcript_path": str(self.transcript(ticket)), "hook_event_name": "UserPromptSubmit"}
        self.afk("hook", "prompt", stdin=json.dumps(payload), env=self.worker_env(ticket))

    def test_an_answered_question_puts_the_worker_back_to_work(self):
        for state in ("question", "blocked"):
            with self.subTest(state):
                self.report(state, "Which database?")
                self.stop()
                self.afk("tick")

                self.answer()
                self.afk("tick")

                self.assertEqual((self.phase(), self.status()["state"]), ("implement", "working"))
                self.assertEqual(self.window(), ("03-widget-ui", "working"))

    def test_a_nudged_silently_stalled_worker_is_back_at_work(self):
        self.stall()
        self.afk("tick")
        self.assertEqual(self.status()["state"], "attention")

        self.answer()
        self.afk("tick")

        self.assertEqual((self.phase(), self.status()["state"]), ("implement", "working"))

    def test_time_waiting_on_an_answer_does_not_count_against_the_phase_limit(self):
        # A question answered hours later, then done reported before the worker's session stops.
        self.limit(phase_minutes=90)
        self.report("question", "Which database?")
        self.stop()
        self.afk("tick")
        self.now += 12 * 3600

        self.answer()
        self.active()
        self.report("done", "Implemented")
        self.afk("tick")
        self.stop()
        self.afk("tick")

        self.assertEqual((self.phase(), self.status()["state"]), ("verify", "working"))
        self.assertEqual(self.interrupts(), [])

    def test_an_answered_worker_that_runs_past_the_limit_again_is_stuck_again(self):
        self.limit(phase_minutes=5)
        self.now += 6 * 60
        self.active()
        self.afk("tick")
        self.assertEqual(self.status()["state"], "stuck")

        self.answer()
        self.afk("tick")
        self.assertEqual(self.status()["state"], "working")
        self.now += 6 * 60
        self.active()
        self.afk("tick")

        self.assertEqual(self.status()["state"], "stuck")
        self.assertEqual(len(self.interrupts()), 2)

    def test_feedback_typed_into_a_review_worker_missing_its_pr_does_not_clear_the_attention(self):
        self.reach_review()
        self.github(None)
        self.now += 60
        self.afk("tick")
        self.assertEqual(self.status()["state"], "attention")

        self.answer()

        self.assertEqual(self.status()["state"], "attention")

    def test_a_worker_whose_transcript_goes_quiet_past_the_idle_limit_is_interrupted_and_stuck(self):
        self.limit(idle_minutes=10)
        self.active()
        self.now += 9 * 60
        self.afk("tick")
        self.assertEqual(self.status()["state"], "working")

        self.now += 2 * 60
        self.afk("tick")

        self.assertEqual(self.status()["state"], "stuck")
        self.assertIn("idle", self.status()["message"])
        self.assertEqual(len(self.interrupts()), 1)

    def test_a_worker_writing_to_its_transcript_is_not_idle(self):
        self.limit(idle_minutes=10)
        for _ in range(3):
            self.now += 9 * 60
            self.active()
            self.afk("tick")

        self.assertEqual(self.status()["state"], "working")

    def pane_gone(self, pane):
        """tmux no longer has `pane`: older tmux fails to find it, tmux 3.6 prints nothing and exits 0."""
        def failing(cmd):
            raise subprocess.CalledProcessError(1, cmd, stderr=f"can't find pane: {pane}")

        return {"failing": failing, "empty": ""}

    def test_a_killed_agent_pane_needs_attention_instead_of_failing_every_tick(self):
        for how, response in self.pane_gone("%5").items():
            with self.subTest(how):
                self.setUp()
                self.run_fake.responses[("tmux", "display-message", "-p", "-t")] = response
                self.report("done", "Implemented")
                self.stop()

                self.afk("tick")
                self.afk("tick")

                self.assertEqual((self.phase(), self.status()["state"]), ("implement", "attention"))
                self.assertEqual(len(self.desktop_notifications()), 1)


    # Merge detection and cleanup

    def pr_polls(self):
        return self.run_fake.find("gh", "pr", "view")

    def github(self, state, ticket="03", url="https://github.com/acme/widgets/pull/42", ready=False):
        """GitHub has a PR in `state` for the worker's branch, or none at all for state None."""
        def view(cmd):
            if state is None:
                raise subprocess.CalledProcessError(1, cmd, stderr=f'no pull requests found for branch "afk/widgets-{ticket}"')
            return pr_view(state, url, ready)

        self.run_fake.responses[("gh", "pr", "view", f"afk/widgets-{ticket}")] = view

    def test_a_review_workers_pr_is_found_by_its_branch_and_its_url_recorded(self):
        self.reach_review(pr="Opened the PR")
        self.github("OPEN")
        self.now += 60

        self.afk("tick")

        poll = self.pr_polls()[-1]
        self.assertEqual(poll, ["gh", "pr", "view", "afk/widgets-03", "--json", "state,url,labels,createdAt"])
        self.assertEqual(self.run_fake.cwds[self.run_fake.calls.index(poll)], str(self.repo))  # not the worktree, which may be gone
        self.assertEqual((self.phase(), self.status()["state"]), ("review", "review"))
        self.assertEqual(self.status()["pr"], "https://github.com/acme/widgets/pull/42")

    def test_a_pr_is_polled_at_most_once_a_minute(self):
        self.reach_review()

        self.afk("tick")
        self.now += 30
        self.afk("tick")
        self.assertEqual(len(self.pr_polls()), 1)

        self.now += 30
        self.afk("tick")
        self.assertEqual(len(self.pr_polls()), 2)

    def merge(self, shell="bash", worktree_status=""):
        """GitHub reports the PR merged; the shell pane runs `shell` and the worktree has `worktree_status` changes."""
        worktree = str(self.project_dir / "worktrees" / "03")
        self.run_fake.responses[("gh", "pr", "view")] = pr_view("MERGED")
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%6")] = shell + "\n"
        self.run_fake.responses[("git", "-C", worktree, "status")] = worktree_status
        self.now += 60

    def close(self, **merge_options):
        """GitHub reports the PR closed without being merged; the panes and worktree are as for merge()."""
        self.merge(**merge_options)
        self.run_fake.responses[("gh", "pr", "view")] = pr_view("CLOSED")

    def cleanup_commands(self):
        worktree = str(self.project_dir / "worktrees" / "03")
        return (
            self.run_fake.find("tmux", "kill-window"),
            self.run_fake.find("git", "-C", str(self.repo), "worktree", "remove"),
            self.run_fake.find("git", "-C", str(self.repo), "branch", "-D"),
        )

    def test_merged_pr_with_idle_shell_and_clean_worktree_removes_window_worktree_and_branch(self):
        self.reach_review()
        self.merge()

        dashboard = self.afk("tick").splitlines()

        worktree = str(self.project_dir / "worktrees" / "03")
        self.assertEqual(
            self.cleanup_commands(),
            (
                [["tmux", "kill-window", "-t", "%5"]],
                [["git", "-C", str(self.repo), "worktree", "remove", "--force", worktree]],
                [["git", "-C", str(self.repo), "branch", "-D", "afk/widgets-03"]],
            ),
        )
        self.assertEqual(dashboard[1:], [])
        self.assertEqual(self.afk("status").splitlines()[1:], [])
        # afk only reads PRs: it never merges, requests reviewers or otherwise acts on GitHub.
        self.assertEqual({tuple(c[:3]) for c in self.run_fake.find("gh")}, {("gh", "pr", "view")})

    def test_cleaning_up_frees_the_workers_slot_for_the_next_ticket(self):
        self.reach_review()
        self.merge()
        self.afk("tick")
        self.ticket("05-export", "Widget export")

        self.afk("start", "05")

        launch = shlex.split(self.run_fake.find("tmux", "send-keys")[-1][4])
        self.assertIn("AFK_SLOT=1", launch)

    def finish_others(self):
        self.ticket("09-docs", "Widget docs", status="done")

    def test_once_the_last_ticket_merges_the_watcher_closes_the_project_and_stops(self):
        self.finish_others()
        self.reach_review()
        self.merge()
        self.now += afk.FRONTIER_POLL
        out = io.StringIO()

        with mock.patch.object(afk.time, "sleep", side_effect=KeyboardInterrupt):
            code = afk.main(["watch"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out,
                            clock=lambda: self.now)

        self.assertEqual(code, 0)  # it returned without sleeping: the loop broke on its own
        self.assertFalse(self.project_dir.exists())
        self.assertIn("project widgets finished: 2 tickets done", out.getvalue())
        self.assertIn("afk: widgets project widgets finished: 2 tickets done", self.desktop_notifications())
        self.assertIn(["tmux", "set-option", "-u", "-t", "work", "@afk_project"], self.run_fake.calls)

    def test_the_watcher_leaves_a_project_open_while_a_ticket_is_still_blocked(self):
        self.finish_others()
        self.ticket("05-export", "Widget export", blocked_by="07")
        self.ticket("07-import", "Widget import", status="claimed")  # someone else's, still in progress
        self.reach_review()
        self.merge()
        self.afk("tick")
        self.now += afk.FRONTIER_POLL

        self.afk("tick")

        self.assertTrue((self.project_dir / "config.toml").exists())

    def test_the_watcher_exits_cleanly_once_another_process_closes_the_project(self):
        sleeps = []

        def closed_elsewhere(seconds):
            if sleeps:
                raise KeyboardInterrupt  # it ticked on: stop it, so the test fails rather than hangs
            sleeps.append(seconds)
            shutil.rmtree(self.project_dir)
            self.run_fake.responses[("tmux", "show-options")] = "\n"  # `afk close` unbound the session

        out = io.StringIO()
        with mock.patch.object(afk.time, "sleep", side_effect=closed_elsewhere):
            code = afk.main(["watch"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out,
                            clock=lambda: self.now)

        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("afk: the project was closed", out.getvalue())
        self.assertFalse(self.project_dir.exists())  # the watcher didn't recreate it

    def test_a_finished_project_with_a_worktree_left_behind_notifies_once_and_stays_open(self):
        self.reach_review()
        self.merge()
        self.afk("tick")
        (self.project_dir / "worktrees" / "03").mkdir()  # as after a cleanup git didn't finish
        self.finish_others()
        self.now += afk.FRONTIER_POLL

        self.afk("tick")
        self.now += afk.FRONTIER_POLL
        self.afk("tick")

        self.assertTrue((self.project_dir / "config.toml").exists())
        [notification] = [n for n in self.desktop_notifications() if "finished" in n]
        self.assertIn("worktrees/03", notification)

    def test_merged_pr_with_a_busy_shell_pane_is_left_cleanup_pending_and_notifies_once(self):
        self.reach_review()
        self.merge(shell="vim")

        self.afk("tick")
        self.afk("tick")

        self.assertEqual(self.cleanup_commands(), ([], [], []))
        self.assertEqual(self.status()["state"], "cleanup-pending")
        notification = self.desktop_notifications()[-1]
        self.assertIn("03", notification)
        self.assertIn("vim", notification)
        self.assertEqual(len(self.desktop_notifications()), 2)  # PR ready, then cleanup pending

    def test_a_cleanup_pending_worker_keeps_its_columns_apart_on_the_dashboard_and_in_status(self):
        self.reach_review()
        self.merge(shell="vim")

        dashboard = self.afk("tick").splitlines()

        self.assertEqual(dashboard[1].split()[:3], ["03", "review", "cleanup-pending"])
        self.assertEqual(self.afk("status").splitlines()[1].split(None, 3),
                         ["03", "review", "cleanup-pending", "its shell pane is running vim"])

    def test_merged_pr_with_uncommitted_changes_is_left_cleanup_pending_and_notifies(self):
        self.reach_review()
        self.merge(worktree_status=" M src/widget.py\n")

        self.afk("tick")

        self.assertEqual(self.cleanup_commands(), ([], [], []))
        self.assertEqual(self.status()["state"], "cleanup-pending")
        self.assertIn("uncommitted", self.desktop_notifications()[-1])

    def test_merged_pr_whose_shell_pane_is_gone_is_cleaned_up(self):
        # A pane that no longer exists has no human in it, so it can't hold up cleanup.
        for how, response in self.pane_gone("%6").items():
            with self.subTest(how):
                self.setUp()
                self.reach_review()
                self.merge()
                self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%6")] = response

                self.afk("tick")

                self.assertEqual([len(c) for c in self.cleanup_commands()], [1, 1, 1])
                self.assertEqual(self.afk("status").splitlines()[1:], [])

    def test_merged_pr_whose_shell_pane_is_gone_with_uncommitted_changes_is_left_cleanup_pending(self):
        self.reach_review()
        self.merge(worktree_status=" M src/widget.py\n")
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%6")] = ""

        self.afk("tick")

        self.assertEqual(self.cleanup_commands(), ([], [], []))
        self.assertEqual(self.status()["state"], "cleanup-pending")
        self.assertIn("uncommitted", self.desktop_notifications()[-1])

    def test_cleanup_pending_worker_is_cleaned_up_once_the_shell_is_idle_again(self):
        self.reach_review()
        self.merge(shell="vim")
        self.afk("tick")
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%6")] = "bash\n"

        self.afk("tick")

        self.assertEqual([len(c) for c in self.cleanup_commands()], [1, 1, 1])
        self.assertEqual(self.afk("status").splitlines()[1:], [])

    def test_a_merged_worker_whose_worktree_has_submodules_is_cleaned_up(self):
        self.reach_review()
        self.merge()

        def with_submodules(cmd):
            if "--force" not in cmd:
                raise subprocess.CalledProcessError(
                    128, cmd, stderr="fatal: working trees containing submodules cannot be moved or removed")
            return git(cmd)

        self.run_fake.responses[("git", "-C", str(self.repo), "worktree", "remove")] = with_submodules

        self.afk("tick")

        self.assertEqual(self.afk("status").splitlines()[1:], [])
        self.assertFalse((self.project_dir / "worktrees" / "03").exists())

    def test_a_merged_worker_with_changes_in_an_ignored_submodule_is_left_cleanup_pending(self):
        # A submodule set to ignore=all hides its changes from a plain `git status`, and cleanup forces the removal.
        self.reach_review()
        self.merge()
        worktree = str(self.project_dir / "worktrees" / "03")
        self.run_fake.responses[("git", "-C", worktree, "status")] = (
            lambda cmd: " M vendor/lib\n" if "--ignore-submodules=none" in cmd else "")

        self.afk("tick")

        self.assertEqual(self.cleanup_commands(), ([], [], []))
        self.assertIn("uncommitted", self.desktop_notifications()[-1])

    def test_a_merged_worker_whose_branch_is_checked_out_elsewhere_is_cleaned_up_leaving_the_branch(self):
        self.reach_review()
        self.merge()

        def checked_out(cmd):
            raise subprocess.CalledProcessError(
                1, cmd, stderr="error: cannot delete branch 'afk/widgets-03' used by worktree at '/clone'")

        self.run_fake.responses[("git", "-C", str(self.repo), "branch", "-D")] = checked_out

        self.afk("tick")

        self.assertEqual(self.afk("status").splitlines()[1:], [])
        self.assertFalse((self.project_dir / "worktrees" / "03").exists())
        [notification] = [n for n in self.desktop_notifications() if "leaving branch" in n]
        self.assertIn("afk/widgets-03", notification)
        self.assertIn("used by worktree at '/clone'", notification)

    def test_a_worker_left_half_cleaned_up_finishes_cleaning_up(self):
        # An earlier cleanup killed the window and removed the worktree, then failed before removing the worker.
        self.reach_review()
        self.merge()
        shutil.rmtree(self.project_dir / "worktrees" / "03")

        def gone(cmd):
            raise subprocess.CalledProcessError(1, cmd, stderr=f"can't find window: {cmd[-1]}")

        self.run_fake.responses[("tmux", "kill-window")] = gone
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%6")] = gone

        self.afk("tick")

        self.assertEqual(self.afk("status").splitlines()[1:], [])
        self.assertEqual(self.run_fake.find("git", "-C", str(self.repo), "worktree", "remove"), [])
        self.assertEqual(self.run_fake.find("git", "-C", str(self.repo), "worktree", "prune"),
                         [["git", "-C", str(self.repo), "worktree", "prune"]])
        self.assertEqual(len(self.run_fake.find("git", "-C", str(self.repo), "branch", "-D")), 1)

    def test_a_worker_failing_with_an_os_error_fails_alone(self):
        self.ticket("05-export", "Widget export")
        self.run_fake.responses[("tmux", "new-window")] = "%7\n"
        self.afk("start", "05")
        self.reach_review()
        self.report("question", "Which format?", ticket="05")
        self.stop(ticket="05")
        self.now += 60

        def missing(cmd):
            raise FileNotFoundError(2, "No such file or directory", "gh")

        self.run_fake.responses[("gh", "pr", "view", "afk/widgets-03")] = missing

        with self.assertRaises(FileNotFoundError):
            afk.main(["tick"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=io.StringIO(),
                     clock=lambda: self.now)

        self.assertTrue([n for n in self.desktop_notifications() if "Which format?" in n])

    def test_the_watcher_reports_a_failed_tick_and_keeps_watching(self):
        self.reach_review()
        self.now += 60

        def missing(cmd):
            raise FileNotFoundError(2, "No such file or directory", "gh")

        self.run_fake.responses[("gh", "pr", "view")] = missing
        polls = len(self.pr_polls())
        out = io.StringIO()

        with mock.patch.object(afk.time, "sleep", side_effect=[None, KeyboardInterrupt]):
            code = afk.main(["watch"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out,
                            clock=lambda: self.now)

        self.assertEqual(code, 0)
        self.assertEqual(len(self.pr_polls()) - polls, 2)  # the failed poll wasn't recorded, so it's retried
        self.assertIn("No such file or directory", out.getvalue())

    def test_closed_pr_with_idle_shell_and_clean_worktree_is_cleaned_up_and_its_ticket_offered_again(self):
        self.reach_review()
        self.close()

        self.afk("tick")

        self.assertEqual([len(c) for c in self.cleanup_commands()], [1, 1, 1])
        self.assertEqual(self.afk("status").splitlines()[1:], [])
        self.assertTrue(any("03 closed and cleaned up" in n for n in self.desktop_notifications()))
        self.assertIn("03  Widget UI", self.afk("frontier").splitlines())

    def test_closed_pr_with_a_busy_shell_pane_waits_then_is_cleaned_up_and_its_ticket_offered_again(self):
        self.reach_review()
        self.close(shell="vim")
        self.afk("tick")
        self.afk("tick")
        self.assertEqual(self.cleanup_commands(), ([], [], []))
        self.assertEqual(self.status()["state"], "cleanup-pending")
        self.assertEqual([n for n in self.desktop_notifications() if "03 closed; cleanup pending" in n and "vim" in n], [self.desktop_notifications()[-1]])
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%6")] = "bash\n"

        self.afk("tick")

        self.assertEqual([len(c) for c in self.cleanup_commands()], [1, 1, 1])
        self.assertIn("03  Widget UI", self.afk("frontier").splitlines())

    def test_a_closed_pr_reopened_while_cleanup_is_pending_keeps_its_worker(self):
        self.reach_review()
        self.close(shell="vim")
        self.afk("tick")
        self.assertEqual(self.status()["state"], "cleanup-pending")
        self.run_fake.responses[("gh", "pr", "view")] = pr_view("OPEN")
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%6")] = "bash\n"
        self.now += 60

        self.afk("tick")
        self.now += 60
        self.afk("tick")

        self.assertEqual(self.cleanup_commands(), ([], [], []))
        self.assertEqual((self.phase(), self.status()["state"]), ("review", "review"))

    def reach_pr(self):
        for message in ["Implemented", "Verified", "Checks pass"]:
            self.finish_phase(message)

    def test_a_blocked_worker_whose_pr_the_human_opened_and_merged_is_cleaned_up(self):
        self.reach_pr()
        self.report("blocked", "Couldn't run /open-pr")
        self.stop()
        self.afk("tick")
        self.merge()

        self.afk("tick")

        self.assertEqual([len(c) for c in self.cleanup_commands()], [1, 1, 1])
        self.assertIn("03 merged", self.desktop_notifications()[-1])

    def test_a_blocked_worker_whose_pr_the_human_made_ready_moves_to_review_with_its_prompt(self):
        self.reach_pr()
        self.github(None)
        self.report("blocked", "Couldn't run /open-pr")
        self.stop()
        self.afk("tick")
        self.github("OPEN", ready=True)
        self.now += 60

        self.afk("tick")

        self.assertEqual((self.phase(), self.status()["state"]), ("review", "review"))
        self.assertIn("AFK phase: review", self.prompts_sent()[-1])
        self.assertIn("03 PR ready for review: https://github.com/acme/widgets/pull/42", self.desktop_notifications()[-1])

    def test_a_blocked_worker_whose_pr_is_open_but_not_ready_stays_blocked(self):
        self.reach_pr()
        self.github("OPEN")
        self.report("blocked", "A check fails on base too")
        self.stop()
        self.now += 60

        self.afk("tick")

        self.assertEqual((self.phase(), self.status()["state"]), ("pr", "blocked"))
        self.assertNotIn("AFK phase: review", self.prompts_sent()[-1])

    def test_a_worker_that_reported_blocked_but_has_not_stopped_is_not_prompted_until_it_stops(self):
        self.reach_pr()
        self.github("OPEN", ready=True)
        self.report("blocked", "Couldn't run /open-pr")
        self.now += 60
        prompts = len(self.prompts_sent())

        self.afk("tick")

        self.assertEqual((self.phase(), self.status()["state"]), ("pr", "blocked"))
        self.assertEqual(len(self.prompts_sent()), prompts)

        self.stop()
        self.now += 60
        self.afk("tick")

        self.assertEqual((self.phase(), self.status()["state"]), ("review", "review"))

    def test_a_worker_waiting_between_open_pr_turns_stays_in_pr_without_needing_attention(self):
        # /open-pr opened the PR, armed its monitors and ended its turn to wait on checks and reviewers.
        self.reach_pr()
        self.github("OPEN")
        notifications = len(self.desktop_notifications())
        self.stop()
        self.now += 60

        self.afk("tick")

        self.assertEqual((self.phase(), self.status()["state"]), ("pr", "working"))
        self.assertNotIn("AFK phase: review", self.prompts_sent()[-1])
        self.assertEqual(len(self.desktop_notifications()), notifications)

    def test_a_worker_that_stops_once_open_pr_labelled_its_pr_ready_moves_to_review(self):
        self.reach_pr()
        self.github("OPEN", ready=True)
        self.stop()
        self.now += 60

        self.afk("tick")

        self.assertEqual((self.phase(), self.status()["state"]), ("review", "review"))
        self.assertIn("AFK phase: review", self.prompts_sent()[-1])

    def test_a_worker_that_resumed_and_labelled_its_pr_is_not_prompted_until_it_stops_again(self):
        # /open-pr stopped to wait on reviewers, resumed when they posted, and labelled the PR Ready before reporting.
        self.reach_pr()
        self.github("OPEN")
        self.stop()
        self.now += 60
        self.afk("tick")
        self.resume()
        self.github("OPEN", ready=True)
        self.now += 60
        prompts = len(self.prompts_sent())

        self.afk("tick")

        self.assertEqual((self.phase(), self.status()["state"]), ("pr", "working"))
        self.assertEqual(len(self.prompts_sent()), prompts)

        self.stop()
        self.now += 60
        self.afk("tick")

        self.assertEqual((self.phase(), self.status()["state"]), ("review", "review"))

    def test_a_worker_still_running_open_pr_stays_in_pr_while_its_pr_is_open(self):
        self.reach_pr()
        self.github("OPEN")
        self.now += 60

        self.afk("tick")

        self.assertEqual(len(self.pr_polls()), 1)
        self.assertEqual((self.phase(), self.status()["state"]), ("pr", "working"))

    def test_a_review_worker_whose_branch_has_no_pr_needs_attention_until_one_is_found(self):
        self.reach_review()
        self.github(None)

        for _ in range(2):
            self.now += 60
            self.afk("tick")

        self.assertEqual((self.phase(), self.status()["state"]), ("review", "attention"))
        self.assertEqual(len(self.desktop_notifications()), 2)  # PR ready, then no PR found
        self.assertIn("03 needs attention: no PR found", self.desktop_notifications()[-1])

        self.github("OPEN")
        self.now += 60
        self.afk("tick")

        self.assertEqual((self.phase(), self.status()["state"]), ("review", "review"))
        self.assertEqual(len(self.desktop_notifications()), 2)

    def test_a_missing_pr_does_not_replace_a_review_workers_own_question(self):
        self.reach_review()
        self.report("question", "Should the endpoint be versioned?")
        self.stop()
        self.github(None)
        self.now += 60
        self.afk("tick")

        self.github("OPEN")
        self.now += 60
        self.afk("tick")

        self.assertEqual(self.status()["state"], "question")
        self.assertEqual(self.status()["message"], "Should the endpoint be versioned?")

    def test_cleaning_up_a_merged_worker_prompts_for_the_next_batch(self):
        self.reach_review()
        self.merge()

        self.afk("tick")

        notification = self.desktop_notifications()[-1]
        self.assertIn("03 merged", notification)
        self.assertIn("/afk next", notification)

    def test_a_failing_pr_poll_does_not_hold_up_the_other_workers(self):
        self.reach_review()
        self.ticket("05-export", "Widget export")
        self.run_fake.responses[("tmux", "new-window")] = "%7\n"
        self.afk("start", "05")
        self.report("done", "Implemented", ticket="05")
        self.stop(ticket="05")

        def offline(cmd):
            raise subprocess.CalledProcessError(1, cmd, stderr="error connecting to api.github.com")

        self.run_fake.responses[("gh", "pr", "view")] = offline
        self.now += 60
        out = io.StringIO()
        code = afk.main(["tick"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out, clock=lambda: self.now)

        self.assertNotEqual(code, 0)
        self.assertIn("api.github.com", out.getvalue())
        self.assertEqual((self.phase("03"), self.phase("05")), ("review", "verify"))

    def test_merged_worker_started_before_shell_panes_were_recorded_is_left_cleanup_pending(self):
        self.reach_review()
        path = self.project_dir / "workers" / "03" / "status.json"
        status = self.status()
        del status["shell_pane"]
        path.write_text(json.dumps(status))
        self.merge()

        self.afk("tick")

        self.assertEqual(self.cleanup_commands(), ([], [], []))
        self.assertEqual(self.status()["state"], "cleanup-pending")
        self.assertIn("shell pane is unknown", self.desktop_notifications()[-1])

    # HITL tickets

    def start_hitl(self):
        """Start ticket 04, of the built-in hitl type, in panes %7 (agent) and %8 (shell)."""
        self.ticket("04-spike", "Widget spike", type="hitl")
        self.run_fake.responses[("tmux", "new-window")] = "%7\n"
        self.run_fake.responses[("tmux", "split-window")] = "%8\n"
        self.afk("start", "04")
        self.calls_before = len(self.run_fake.calls)

    def test_a_hitl_ticket_is_set_up_like_any_other_but_launches_a_guide_mode_agent(self):
        self.start_hitl()

        worktree = str(self.project_dir / "worktrees" / "04")
        self.assertIn(["git", "-C", str(self.repo), "worktree", "add", "-b", "afk/widgets-04", worktree, "main"], self.run_fake.calls)
        [split] = [c for c in self.run_fake.find("tmux", "split-window") if "%7" in c]
        launch = shlex.split(self.run_fake.find("tmux", "send-keys")[-1][4])
        self.assertIn("AFK_SLOT=2", launch)
        claude = launch[launch.index("claude") :]
        self.assertEqual(claude[1:3], ["--permission-mode", "auto"])
        prompt = claude[-1]
        self.assertTrue(prompt.startswith("AFK guide mode — ticket 04"), prompt)
        self.assertIn(str(self.scratch / "issues" / "04-spike.md"), prompt)
        self.assertNotIn("AFK phase: implement", prompt)
        self.assertIn("opens a PR from this branch against `main`", prompt)
        self.assertNotIn("afk land", prompt)  # a project landing by PR lands hitl tickets by PR too

    def test_the_watcher_never_drives_a_hitl_worker_and_shows_it_as_the_users(self):
        self.start_hitl()
        self.stop("04")
        self.afk("tick")
        self.report("done", "Finished the spike", ticket="04")
        self.stop("04")
        self.afk("tick")
        self.now += 3 * 3600  # well past every phase and idle limit

        dashboard = self.afk("tick").splitlines()

        self.assertEqual(self.prompts_sent("%7"), [])
        self.assertEqual(self.run_fake.find("tmux", "send-keys", "-t", "%7", "Escape"), [])
        self.assertEqual([n for n in self.desktop_notifications() if " 04 " in n], [])
        self.assertEqual(dashboard[2].split()[:3], ["04", "hitl", "yours"])

    def test_hitl_workers_do_not_count_toward_max_workers(self):
        with open(self.project_dir / "config.toml", "a") as config:
            config.write("\n[limits]\nmax_workers = 1\n")

        self.start_hitl()  # 03 already fills the one AFK slot

        self.reach_review()  # 03 parks on the human, leaving only the hitl worker active
        self.ticket("05-export", "Widget export")
        self.afk("start", "05")
        self.assertEqual(self.status("05")["state"], "working")

    def test_hitl_workers_leave_afk_next_capacity_free(self):
        self.start_hitl()  # beside 03, under the default max_workers of 3

        self.assertEqual(json.loads(self.afk("next"))["capacity"], 2)

    def test_a_merged_hitl_pr_is_found_by_its_branch_and_cleaned_up_once_safe(self):
        self.start_hitl()
        worktree = str(self.project_dir / "worktrees" / "04")
        self.run_fake.responses[("gh", "pr", "view", "afk/widgets-04")] = pr_view("MERGED")
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%8")] = "vim\n"
        self.run_fake.responses[("git", "-C", worktree, "status")] = ""

        self.afk("tick")

        [poll] = self.pr_polls()
        self.assertEqual(poll, ["gh", "pr", "view", "afk/widgets-04", "--json", "state,url,labels,createdAt"])
        self.assertEqual(self.run_fake.cwds[self.run_fake.calls.index(poll)], str(self.repo))
        self.assertEqual(self.status("04")["state"], "cleanup-pending")
        self.assertIn("04 merged; cleanup pending", self.desktop_notifications()[-1])

        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%8")] = "bash\n"
        self.afk("tick")

        self.assertEqual(self.run_fake.find("tmux", "kill-window"), [["tmux", "kill-window", "-t", "%7"]])
        self.assertEqual(self.run_fake.find("git", "-C", str(self.repo), "worktree", "remove"), [["git", "-C", str(self.repo), "worktree", "remove", "--force", worktree]])
        self.assertEqual(self.run_fake.find("git", "-C", str(self.repo), "branch", "-D"), [["git", "-C", str(self.repo), "branch", "-D", "afk/widgets-04"]])
        self.assertFalse((self.project_dir / "workers" / "04").exists())

    def test_a_hitl_branch_without_a_pr_yet_is_polled_once_a_minute_without_failing(self):
        self.start_hitl()

        def no_pr(cmd):
            raise subprocess.CalledProcessError(1, cmd, stderr='no pull requests found for branch "afk/widgets-04"')

        self.run_fake.responses[("gh", "pr", "view", "afk/widgets-04")] = no_pr

        self.afk("tick")
        self.now += 30
        self.afk("tick")
        self.assertEqual(len(self.pr_polls()), 1)
        self.now += 30
        self.afk("tick")

        self.assertEqual(len(self.pr_polls()), 2)
        self.assertEqual((self.phase("04"), self.status("04")["state"]), ("hitl", "yours"))

    def test_a_restarted_worker_is_not_cleaned_up_for_the_pr_an_earlier_attempt_closed(self):
        self.start_hitl()
        self.run_fake.responses[("gh", "pr", "view", "afk/widgets-04")] = pr_view("CLOSED", created=self.now)
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%8")] = "bash\n"
        self.afk("tick")
        self.assertFalse((self.project_dir / "workers" / "04").exists())
        self.now += 60

        self.afk("start", "04")  # the closed PR released it onto the frontier; its branch is recreated
        self.now += 60
        self.afk("tick")

        self.assertEqual((self.phase("04"), self.status("04")["state"]), ("hitl", "yours"))

    def test_a_worker_whose_ticket_is_gone_from_the_tracker_does_not_stop_the_watcher(self):
        self.start_hitl()
        (self.scratch / "issues" / "04-spike.md").unlink()

        self.afk("tick")

        self.assertEqual((self.phase("04"), self.status("04")["state"]), ("hitl", "yours"))

    def test_a_worker_whose_ticket_is_gone_is_still_cleaned_up_when_its_pr_closes(self):
        self.start_hitl()
        (self.scratch / "issues" / "04-spike.md").unlink()
        self.run_fake.responses[("gh", "pr", "view", "afk/widgets-04")] = pr_view("CLOSED")
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%8")] = "bash\n"

        self.afk("tick")

        self.assertEqual(self.run_fake.find("tmux", "kill-window"), [["tmux", "kill-window", "-t", "%7"]])
        self.assertFalse((self.project_dir / "workers" / "04").exists())

    def test_a_worker_whose_ticket_is_closed_with_unpushed_commits_is_left_cleanup_pending(self):
        self.start_hitl()
        worktree = str(self.project_dir / "worktrees" / "04")
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%8")] = "bash\n"
        self.run_fake.responses[("git", "-C", worktree, "log")] = "abc1234 Try the spike\n"
        path = self.scratch / "issues" / "04-spike.md"
        path.write_text(path.read_text().replace("**Status:** ready-for-agent", "**Status:** closed"))

        self.afk("tick")

        self.assertEqual(self.run_fake.find("tmux", "kill-window"), [])
        self.assertEqual(self.status("04")["state"], "cleanup-pending")
        self.assertIn("unpushed commits", self.desktop_notifications()[-1])

    def test_a_hitl_worker_whose_ticket_is_closed_without_a_pr_is_cleaned_up_once_safe(self):
        self.start_hitl()

        def no_pr(cmd):
            raise subprocess.CalledProcessError(1, cmd, stderr='no pull requests found for branch "afk/widgets-04"')

        self.run_fake.responses[("gh", "pr", "view", "afk/widgets-04")] = no_pr
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%8")] = "bash\n"
        path = self.scratch / "issues" / "04-spike.md"
        path.write_text(path.read_text().replace("**Status:** ready-for-agent", "**Status:** closed"))

        self.afk("tick")

        worktree = str(self.project_dir / "worktrees" / "04")
        self.assertEqual(self.run_fake.find("tmux", "kill-window"), [["tmux", "kill-window", "-t", "%7"]])
        self.assertEqual(self.run_fake.find("git", "-C", str(self.repo), "worktree", "remove"), [["git", "-C", str(self.repo), "worktree", "remove", "--force", worktree]])
        self.assertEqual(self.run_fake.find("git", "-C", str(self.repo), "branch", "-D"), [["git", "-C", str(self.repo), "branch", "-D", "afk/widgets-04"]])
        self.assertFalse((self.project_dir / "workers" / "04").exists())
        self.assertIn("04 ticket closed and cleaned up", self.desktop_notifications()[-1])
        self.assertIn("**Status:** closed", path.read_text())  # the tracker already has it as closed


class TokenCapTest(WorkerTestCase):
    """Token caps, with usage summed from fixture transcripts at the paths the worker's hooks record."""

    def spend(self, tokens, ago=0, ticket="03", session=None, subagent=None):
        """The worker's session, or its `subagent`, gets one model response using `tokens`, `ago` seconds before now,
        written to its transcript as Claude Code writes it: a line per content block, each repeating the response's
        usage. Then a hook of the session records the session's transcript."""
        session = session or self.transcript(ticket)
        path = session.with_suffix("") / "subagents" / f"agent-{subagent}.jsonl" if subagent else session
        model_response(path, tokens, self.now - ago)
        payload = {"session_id": "s", "transcript_path": str(session), "hook_event_name": "PreToolUse"}
        self.afk("hook", "activity", stdin=json.dumps(payload), env=self.worker_env(ticket))

    def test_a_worker_over_the_phase_token_cap_is_interrupted_stuck_and_notified(self):
        self.limit(phase_tokens=25_000)
        self.spend(10_000)
        self.spend(10_000)
        self.afk("tick")
        self.assertEqual(self.status()["state"], "working")

        self.spend(10_000)
        self.afk("tick")

        self.assertEqual(self.status()["state"], "stuck")
        self.assertIn("over 25,000 tokens in implement", self.status()["message"])
        self.assertEqual(len(self.interrupts()), 1)
        [notification] = self.desktop_notifications()
        self.assertIn("03 stuck: over 25,000 tokens in implement", notification)

    def test_tokens_used_in_an_earlier_phase_do_not_count_against_the_next(self):
        self.limit(phase_tokens=25_000)
        self.spend(20_000)
        self.now += 60
        self.report("done", "Implemented")
        self.stop()
        self.afk("tick")
        self.assertEqual(self.phase(), "verify")

        self.now += 60
        self.spend(10_000)
        self.afk("tick")

        self.assertEqual(self.status()["state"], "working")

    def test_tokens_used_by_the_workers_subagents_count(self):
        self.limit(phase_tokens=25_000)
        self.spend(10_000)
        self.spend(10_000, subagent="a1")
        self.spend(10_000, subagent="b2")

        self.afk("tick")

        self.assertEqual(self.status()["state"], "stuck")

    def test_tokens_used_in_every_session_the_hooks_recorded_count(self):
        # The worker's agent is relaunched mid-phase, in a session with a transcript of its own.
        self.limit(phase_tokens=25_000)
        self.spend(20_000)
        self.spend(10_000, session=self.tmp / "relaunched.jsonl")

        self.afk("tick")

        self.assertEqual(self.status()["state"], "stuck")

    def test_the_default_token_caps_apply_without_config(self):
        self.spend(29_000_000)
        self.afk("tick")
        self.assertEqual(self.status()["state"], "working")
        self.spend(2_000_000)
        self.afk("tick")
        self.assertEqual(self.status()["message"], "over 30,000,000 tokens in implement")

        self.start_another()
        self.spend(200_000_000, ago=3600, ticket="05")  # before its phase, but within the day
        self.afk("tick")
        self.assertEqual(self.status("05")["state"], "working")
        self.spend(20_000_000, ticket="05")
        self.afk("tick")
        self.assertEqual(self.status("05")["message"], "workers over 250,000,000 tokens in the last 24h")

    def test_the_watcher_counts_a_response_once_its_half_written_line_is_finished_skipping_what_isnt_one(self):
        self.limit(phase_tokens=25_000)
        model_response(self.transcript(), 20_000, self.now)
        response = self.transcript().read_text().splitlines()[-2]  # its last block, before its tool's result
        response = response.replace('"msg_', '"msg_late_')  # a second response of the same size
        with open(self.transcript(), "a") as transcript:
            transcript.write('{"type": "summary", "summary": "Widget UI"}\n'
                             '{"type": "assistant", "timestamp": "1970-01-12T13:46:40.000Z", '
                             '"message": {"id": "msg_x", "usage": null}}\n'
                             'not json\n' + response[:40])
        self.resume()  # a hook records the transcript
        ticks = []

        def sleep(seconds):
            ticks.append(self.status()["state"])
            if len(ticks) == 2:
                raise KeyboardInterrupt
            with open(self.transcript(), "a") as transcript:
                transcript.write(response[40:] + "\n")

        with mock.patch.object(afk.time, "sleep", side_effect=sleep):
            out = io.StringIO()
            afk.main(["watch"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out, clock=lambda: self.now)

        self.assertNotIn("failed", out.getvalue())
        self.assertEqual(ticks, ["working", "stuck"])

    def start_another(self, ticket="05"):
        self.ticket(f"{ticket}-export", "Widget export")
        self.run_fake.responses[("tmux", "new-window")] = "%7\n"
        self.afk("start", ticket)

    def test_workers_together_over_the_daily_token_cap_are_each_interrupted_stuck_and_notified(self):
        self.limit(daily_tokens=25_000)
        self.start_another()
        self.spend(15_000)
        self.afk("tick")
        self.assertEqual(self.status()["state"], "working")

        self.spend(15_000, ticket="05")
        self.afk("tick")

        for ticket, pane in (("03", "%5"), ("05", "%7")):
            self.assertEqual(self.status(ticket)["state"], "stuck")
            self.assertEqual(len(self.interrupts(pane)), 1)
        self.assertEqual(self.desktop_notifications()[-2:], [
            f"afk: widgets {ticket} stuck: workers over 25,000 tokens in the last 24h" for ticket in ("03", "05")])

    def test_only_tokens_used_in_the_last_24_hours_count_against_the_daily_cap(self):
        self.limit(daily_tokens=25_000)
        self.start_another()
        self.spend(20_000, ago=25 * 3600)
        self.spend(10_000, ticket="05")

        self.afk("tick")

        self.assertEqual([self.status(ticket)["state"] for ticket in ("03", "05")], ["working", "working"])

    def test_starting_a_ticket_is_refused_while_the_project_is_over_its_daily_token_cap(self):
        self.limit(daily_tokens=25_000)
        self.spend(30_000)
        self.afk("tick")
        self.ticket("05-export", "Widget export")
        out = io.StringIO()

        code = afk.main(["start", "05"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out,
                        clock=lambda: self.now)

        self.assertEqual(code, 1)
        self.assertIn("over 25,000 tokens in the last 24h", out.getvalue())
        self.assertIn("daily_tokens", out.getvalue())
        self.assertFalse((self.project_dir / "workers" / "05").exists())
        self.assertIn("**Status:** ready-for-agent", (self.scratch / "issues" / "05-export.md").read_text())

    def test_starting_a_ticket_counts_the_running_workers_tokens_since_the_last_tick(self):
        self.limit(daily_tokens=25_000)
        self.spend(30_000)  # no tick since
        self.ticket("05-export", "Widget export")
        out = io.StringIO()

        code = afk.main(["start", "05"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out,
                        clock=lambda: self.now)

        self.assertEqual(code, 1)
        self.assertIn("over 25,000 tokens in the last 24h", out.getvalue())

    def test_tokens_used_by_a_worker_since_cleaned_up_count_against_the_daily_cap(self):
        self.limit(daily_tokens=25_000)
        self.spend(20_000)
        for message in ["Implemented", "Verified", "Checks pass", "https://github.com/acme/widgets/pull/42"]:
            self.finish_phase(message)
        self.run_fake.responses[("gh", "pr", "view")] = pr_view("MERGED")
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%6")] = "bash\n"
        self.now += 60
        self.afk("tick")
        self.assertFalse((self.project_dir / "workers" / "03").exists())

        self.start_another()
        self.spend(10_000, ticket="05")
        self.afk("tick")

        self.assertEqual(self.status("05")["state"], "stuck")


class LocalLandingWatchTest(WorkerTestCase):
    INIT = ("--land", "local", "--target", "widgets-v2")

    def test_each_done_phase_advances_to_the_next_critiquing_instead_of_opening_a_pr(self):
        for _ in range(4):
            self.finish_phase()

        self.assertEqual(self.phase(), "review")
        self.assertEqual(self.status()["state"], "review")
        self.assertEqual(
            [p.splitlines()[0].split(" — ")[0] for p in self.prompts_sent()],
            ["AFK phase: verify", "AFK phase: prepr", "AFK phase: critique", "AFK phase: review"],
        )

    def test_prepr_checks_for_merge_conflicts_against_the_local_target(self):
        for _ in range(2):
            self.finish_phase()

        prepr = self.prompts_sent()[-1]
        self.assertTrue(prepr.startswith("AFK phase: prepr"), prepr)
        self.assertIn("git merge widgets-v2", prepr)
        self.assertIn("git diff widgets-v2...HEAD", prepr)
        self.assertNotIn("git fetch", prepr)
        self.assertNotIn("{{", prepr)

    def test_reaching_review_notifies_that_the_branch_is_ready(self):
        for _ in range(4):
            self.finish_phase()

        self.assertIn("03 branch ready for review", self.desktop_notifications()[-1])
        self.assertNotIn("PR", self.desktop_notifications()[-1])

    def test_critique_runs_each_of_the_repos_reviewers_over_the_diff_against_the_target(self):
        self.repo_config('reviewers = ["coderabbit review --base {{target}} --plain", "copilot -p \'review git diff {{target}}...HEAD\'"]')
        for _ in range(3):
            self.finish_phase()

        critique = self.prompts_sent()[-1]
        self.assertTrue(critique.startswith("AFK phase: critique"), critique)
        self.assertIn("`coderabbit review --base widgets-v2 --plain`", critique)
        self.assertIn("`copilot -p 'review git diff widgets-v2...HEAD'`", critique)
        self.assertIn("git diff widgets-v2...HEAD", critique)
        self.assertNotIn("{{", critique)

    def test_critique_without_reviewers_is_a_code_review_subagent(self):
        for _ in range(3):
            self.finish_phase()

        critique = self.prompts_sent()[-1]
        self.assertIn("No reviewers are configured", critique)
        self.assertIn("code-review subagent", critique)
        self.assertNotIn("{{", critique)

    def test_a_critique_round_that_fixed_findings_is_followed_by_another_until_one_finds_nothing(self):
        for _ in range(3):
            self.finish_phase()

        self.finish_phase("round: fixed a missing null check")

        self.assertEqual((self.phase(), self.status()["state"]), ("critique", "working"))
        self.assertTrue(self.prompts_sent()[-1].startswith("AFK phase: critique"))

        self.finish_phase("Critique clean; rejected 2 findings")

        self.assertEqual((self.phase(), self.status()["state"]), ("review", "review"))
        self.assertEqual(
            [p.splitlines()[0].split(" — ")[0] for p in self.prompts_sent()][-3:],
            ["AFK phase: critique", "AFK phase: critique", "AFK phase: review"],
        )

    def test_the_review_prompt_critiques_feedback_fixes_and_lands_on_approval_without_pushing(self):
        self.repo_config('reviewers = ["coderabbit review --base {{target}} --plain"]')
        for _ in range(4):
            self.finish_phase()

        review = self.prompts_sent()[-1]
        self.assertTrue(review.startswith("AFK phase: review"), review)
        self.assertIn("`coderabbit review --base widgets-v2 --plain`", review)
        self.assertIn("git merge widgets-v2", review)
        self.assertIn('afk land "', review)
        self.assertIn("Ticket: 03", review)
        self.assertIn("Never push", review)
        self.assertNotIn("origin/", review)
        self.assertNotIn("{{", review)

    def test_a_worker_in_review_is_never_looked_up_on_github(self):
        for _ in range(4):
            self.finish_phase()

        for _ in range(3):
            self.now += afk.PR_POLL_SECONDS
            self.afk("tick")

        self.assertEqual(self.run_fake.find("gh", "pr", "view"), [])
        self.assertEqual(self.status()["state"], "review")

    def test_a_worker_in_review_whose_ticket_is_closed_ends_without_a_pr_until_it_is_reopened(self):
        for _ in range(4):
            self.finish_phase()
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%6")] = "bash\n"
        self.run_fake.responses[("git", "-C", str(self.project_dir / "worktrees" / "03"), "log")] = "abc1234 Widget UI\n"
        path = self.scratch / "issues" / "03-ui.md"
        path.write_text(path.read_text().replace("**Status:** ready-for-agent", "**Status:** closed"))

        self.afk("tick")

        self.assertEqual(self.run_fake.find("gh", "pr", "view"), [])
        self.assertEqual(self.status()["state"], "cleanup-pending")
        self.assertEqual(self.status()["ended"], "ticket closed")

        path.write_text(path.read_text().replace("**Status:** closed", "**Status:** ready-for-agent"))
        self.now += afk.PR_POLL_SECONDS
        self.afk("tick")

        self.assertEqual(self.status()["state"], "review")
        self.assertNotIn("ended", self.status())

    def test_a_hitl_workers_pr_is_still_found_by_its_branch_and_cleaned_up_once_merged(self):
        self.ticket("04-spike", "Widget spike", type="hitl")
        self.run_fake.responses[("tmux", "new-window")] = "%7\n"
        self.run_fake.responses[("tmux", "split-window")] = "%8\n"
        self.afk("start", "04")

        self.afk("tick")

        self.assertEqual([c[3] for c in self.run_fake.find("gh", "pr", "view")], ["afk/widgets-04"])
        self.assertEqual(self.status("04")["state"], "yours")

        self.run_fake.responses[("gh", "pr", "view", "afk/widgets-04")] = pr_view("MERGED")
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%8")] = "bash\n"
        self.run_fake.responses[("git", "-C", str(self.project_dir / "worktrees" / "04"), "status")] = ""
        self.now += afk.PR_POLL_SECONDS
        self.afk("tick")

        self.assertIn(["tmux", "kill-window", "-t", "%7"], self.run_fake.find("tmux", "kill-window"))
        self.assertIn(["git", "-C", str(self.repo), "branch", "-D", "afk/widgets-04"], self.run_fake.find("git", "-C", str(self.repo), "branch", "-D"))
        self.assertFalse((self.project_dir / "workers" / "04").exists())
        self.assertIn("04 merged and cleaned up", " ".join(self.desktop_notifications()))

    def test_the_guide_mode_prompt_lands_on_the_humans_request_after_committing_and_confirming(self):
        self.ticket("04-spike", "Widget spike", type="hitl")
        self.run_fake.responses[("tmux", "new-window")] = "%7\n"
        self.afk("start", "04")

        launch = shlex.split(self.run_fake.find("tmux", "send-keys")[-1][4])
        prompt = launch[-1]
        self.assertTrue(prompt.startswith("AFK guide mode — ticket 04"), prompt)
        self.assertIn('afk land "', prompt)
        self.assertIn("type(scope): summary", prompt)
        self.assertIn("Ticket: 04", prompt)
        self.assertIn("squashes the branch onto `widgets-v2`", prompt)
        self.assertIn("if you committed anything since their request, show the human what will land and ask them to confirm", prompt)
        self.assertIn("in a plain reply, and end your turn", prompt)  # a question tool's answer isn't a typed prompt
        self.assertIn("against `widgets-v2`", prompt)
        self.assertNotIn("{{", prompt)


class ApprovalTest(WorkerTestCase):
    INIT = ("--land", "local", "--target", "widgets-v2")

    def test_a_prompt_the_human_types_is_recorded_as_their_last(self):
        self.now += 300

        self.prompt("lgtm, land it")

        self.assertEqual(self.status()["last_human_prompt_at"], self.now)

    def test_a_phase_prompt_the_watcher_pasted_is_not_the_humans(self):
        self.prompt("lgtm, land it")
        approved = self.now
        self.now += 300
        self.finish_phase()

        # The verify prompt, as Claude Code hands the hook a paste: wrapped, after blank lines.
        self.prompt(f'\n\n<pasted_content id="7eeb">\n{self.prompts_sent()[-1]}</pasted_content id="7eeb">\n')
        self.prompt("/tdd AFK phase: implement — ticket 03 (x)\n\nImplement it.")  # the first, with its skill

        self.assertEqual(self.status()["last_human_prompt_at"], approved)

    def test_a_background_task_landing_is_not_the_humans(self):
        self.prompt("lgtm")
        approved = self.now
        self.now += 300

        self.prompt("<task-notification>\n<task-id>bsscixg0x</task-id>\n<status>completed</status>\n</task-notification>")

        self.assertEqual(self.status()["last_human_prompt_at"], approved)

    def test_a_phase_prompt_pasted_after_half_typed_text_is_not_the_humans(self):
        self.prompt("lgtm")
        approved = self.now
        self.now += 300
        self.finish_phase()

        self.prompt(f'lg\n\n<pasted_content id="7eeb">\n{self.prompts_sent()[-1]}</pasted_content id="7eeb">\n')

        self.assertEqual(self.status()["last_human_prompt_at"], approved)


class GitRun(FakeRun):
    """FakeRun, except git, which runs for real: landing is git's work, so it is checked against real repos."""

    def __call__(self, cmd, input=None, cwd=None):
        if cmd[0] != "git":
            return super().__call__(cmd, input, cwd)
        self.calls.append(list(cmd))
        self.inputs.append(input)
        self.cwds.append(cwd and str(cwd))
        before = self.responses.get(("git", "before"))
        if before:
            before(cmd)
        return afk.default_run(cmd, input, cwd)


class RealGit:
    """A real git clone with an origin at self.repo, in place of the faked git: landing is git's work."""

    def real_git(self):
        identity = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Ada",
                    "GIT_AUTHOR_EMAIL": "ada@example.com", "GIT_COMMITTER_NAME": "Ada", "GIT_COMMITTER_EMAIL": "ada@example.com"}
        patcher = mock.patch.dict(os.environ, identity)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.now = 1_700_000_000.0
        self.run_fake = GitRun(self.run_fake.responses)
        del self.run_fake.responses[("git", "-C")]
        self.origin = self.tmp / "origin.git"
        self.git("init", "-q", "--bare", "-b", "main", str(self.origin), cwd=self.tmp)
        self.git("init", "-q", "-b", "main")
        self.commit("README.md", "widgets\n", "Initial commit", at=self.now - 3600)
        self.git("remote", "add", "origin", str(self.origin))
        self.git("push", "-q", "origin", "main")

    def git(self, *args, cwd=None, at=None):
        env = {**os.environ, **({"GIT_AUTHOR_DATE": f"@{int(at)} +0000", "GIT_COMMITTER_DATE": f"@{int(at)} +0000"} if at else {})}
        return subprocess.run(["git", *args], cwd=cwd or self.repo, env=env, check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, name, text, message, cwd=None, at=None):
        """Commit `text` to file `name`, in the clone or the given worktree, at clock time `at`."""
        cwd = cwd or self.repo
        (Path(cwd) / name).write_text(text)
        self.git("add", name, cwd=cwd)
        self.git("commit", "-q", "-m", message, cwd=cwd, at=at or self.now)
        return self.git("rev-parse", "HEAD", cwd=cwd)


class LandTestCase(RealGit, WorkerTestCase):
    """Worker 03 of a local-mode project, of task type TYPE, in a real git clone with an origin, landing on INIT's target."""

    INIT = ("--land", "local", "--target", "widgets-v2")
    TYPE = None

    def setUp(self):
        AfkTestCase.setUp(self)
        self.real_git()
        self.ticket("03-ui", "Widget UI", type=self.TYPE)
        self.afk("init", str(self.scratch), *self.INIT)
        self.run_fake.responses[("tmux", "split-window")] = "%6\n"
        self.afk("start", "03")
        self.calls_before = len(self.run_fake.calls)
        self.worktree = self.project_dir / "worktrees" / "03"

    def work(self, name="widget.py", text="widget = 1\n", message="Add the widget"):
        """The worker commits on its branch, a minute ago."""
        return self.commit(name, text, message, cwd=self.worktree, at=self.now - 60)

    def other_checkout(self):
        """A second worktree on the target, standing in for another worker that lands on it."""
        path = self.tmp / "other"
        if not path.exists():
            self.git("worktree", "add", "-q", str(path), "widgets-v2")
        return path

    def tip(self, branch, cwd=None):
        return self.git("rev-parse", branch, cwd=cwd)

    def land(self, message="feat(widgets): add the widget\n\nA widget.\n\nTicket: 03\n"):
        """`afk land` from the worker, as (exit code, output)."""
        out = io.StringIO()
        code = afk.main(["land", message], run=self.run_fake, env={**self.env, **self.worker_env()},
                        stdin=io.StringIO(), stdout=out, clock=lambda: self.now)
        return code, out.getvalue()


class LandTest(LandTestCase):
    def test_an_approved_branch_lands_as_one_squash_commit_on_the_target(self):
        head = self.work()
        self.work("widget_test.py", "assert widget\n", "Test the widget")
        before = self.tip("widgets-v2")
        self.prompt("lgtm")

        code, out = self.land()

        self.assertEqual(code, 0, out)
        landed = self.tip("widgets-v2")
        self.assertEqual(self.git("rev-parse", f"{landed}^"), before)
        self.assertEqual(self.git("rev-parse", f"{landed}^{{tree}}"), self.git("rev-parse", "HEAD^{tree}", cwd=self.worktree))
        self.assertEqual(self.git("log", "-1", "--format=%B", landed), "feat(widgets): add the widget\n\nA widget.\n\nTicket: 03")
        self.assertEqual((self.status()["ended"], self.status()["landed_sha"]), ("landed", landed))
        self.assertIn(landed[:7], out)


    def test_a_conflict_free_merge_of_the_target_after_approval_keeps_it(self):
        self.work()
        self.commit("other.py", "other = 1\n", "Another ticket landed", cwd=self.other_checkout())
        self.prompt("lgtm")
        self.now += 120
        self.git("merge", "-q", "--no-edit", "widgets-v2", cwd=self.worktree, at=self.now - 60)

        code, out = self.land()

        self.assertEqual(code, 0, out)
        self.assertEqual(self.git("ls-tree", "--name-only", "widgets-v2").split(), ["README.md", "other.py", "widget.py"])


    def test_a_target_checked_out_in_a_clean_worktree_is_fast_forwarded_there(self):
        checkout = self.other_checkout()
        (checkout / "notes.txt").write_text("untracked, so nothing landing can lose\n")
        self.work()
        self.prompt("lgtm")

        code, out = self.land()

        self.assertEqual(code, 0, out)
        self.assertEqual(self.tip("HEAD", cwd=checkout), self.tip("widgets-v2"))
        self.assertEqual((checkout / "widget.py").read_text(), "widget = 1\n")
        self.assertEqual(self.git("status", "--porcelain", cwd=checkout), "?? notes.txt")


    def test_an_integration_branch_is_pushed_once_landed(self):
        self.work()
        self.prompt("lgtm")

        self.land()

        self.assertEqual(self.tip("widgets-v2", cwd=self.origin), self.tip("widgets-v2"))

    def test_a_failed_push_notifies_without_undoing_the_land(self):
        pushed = self.git("commit-tree", "main^{tree}", "-p", "main", "-m", "Pushed from elsewhere")
        self.git("push", "-q", "origin", f"{pushed}:refs/heads/widgets-v2")  # so landing can't fast-forward origin's
        self.work()
        self.prompt("lgtm")

        code, out = self.land()

        self.assertEqual(code, 0, out)
        self.assertEqual(self.tip("widgets-v2"), self.status()["landed_sha"])
        self.assertEqual(self.tip("widgets-v2", cwd=self.origin), pushed)
        self.assertIn("push", self.run_fake.find("notify-send")[-1][-1])


    def test_the_watcher_leaves_a_landing_worker_alone_until_its_land_has_finished(self):
        self.work()
        self.prompt("lgtm")
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%6")] = "zsh\n"  # an idle shell
        during = []

        def watcher_ticks_mid_push(cmd):
            if "push" in cmd and not during:
                during.append(None)
                self.afk("tick")
                during[0] = (self.project_dir / "workers" / "03").exists()

        self.run_fake.responses[("git", "before")] = watcher_ticks_mid_push
        code, out = self.land()

        self.assertEqual(code, 0, out)
        self.assertEqual(during, [True])
        self.assertEqual(self.tip("widgets-v2", cwd=self.origin), self.status()["landed_sha"])
        self.assertEqual(self.status()["ended"], "landed")

    def test_a_checkout_of_the_target_that_cannot_be_updated_notifies_without_undoing_the_land(self):
        checkout = self.other_checkout()
        (checkout / "widget.py").write_text("untracked, in the way\n")
        self.work()
        self.prompt("lgtm")

        code, out = self.land()

        self.assertEqual(code, 0, out)
        self.assertEqual(self.status()["ended"], "landed")
        self.assertEqual((checkout / "widget.py").read_text(), "untracked, in the way\n")
        self.assertIn(f"{checkout} still has", self.run_fake.find("notify-send")[-1][-1])


    def test_a_checkout_of_the_target_whose_directory_is_gone_does_not_stop_the_land(self):
        shutil.rmtree(self.other_checkout())  # git still lists it, as prunable
        self.work()
        self.prompt("lgtm")

        code, out = self.land()

        self.assertEqual(code, 0, out)
        self.assertEqual(self.status()["ended"], "landed")


class LandOnBaseTest(LandTestCase):
    INIT = ("--land", "local")

    def test_base_is_landed_on_its_checkout_in_the_clone_and_never_pushed(self):
        pushed = self.tip("main", cwd=self.origin)
        self.work()
        self.prompt("lgtm")

        code, out = self.land()

        self.assertEqual(code, 0, out)
        self.assertEqual(self.tip("HEAD"), self.tip("main"))
        self.assertEqual((self.repo / "widget.py").read_text(), "widget = 1\n")
        self.assertEqual(self.tip("main", cwd=self.origin), pushed)
        self.assertEqual([c for c in self.run_fake.find("git") if "push" in c], [])


class LandRefusalTest(LandTestCase):
    def assertRefused(self, message, land_message=None):
        """afk land fails with `message`, leaving the target and the worker's status as they were."""
        before, status = self.tip("widgets-v2"), self.status()
        code, out = self.land(*(() if land_message is None else (land_message,)))
        self.assertEqual(code, 1, out)
        self.assertIn(message, out)
        self.assertEqual(self.tip("widgets-v2"), before)
        self.assertEqual(self.status(), status)

    def test_an_empty_message_is_refused(self):
        self.work()
        self.prompt("lgtm")

        self.assertRefused("afk: land needs a commit message", "  \n")


    def test_a_branch_the_human_has_not_approved_is_refused(self):
        self.work()

        self.assertRefused("no approval since")

    def test_an_approval_followed_by_a_commit_of_the_workers_own_is_stale(self):
        self.prompt("lgtm")
        self.now += 120
        self.work()  # a fix, a minute after the human approved

        self.assertRefused("no approval since")

    def test_a_branch_with_nothing_to_land_is_refused(self):
        self.prompt("lgtm")

        self.assertRefused("nothing to land")


    def test_a_target_that_moved_since_the_branch_last_merged_it_is_refused(self):
        self.work()
        self.commit("other.py", "other = 1\n", "Another ticket landed", cwd=self.other_checkout())
        self.prompt("lgtm")

        self.assertRefused("target moved; merge it, re-run pre-PR, land again")


    def test_a_merge_of_the_target_with_resolved_conflicts_after_approval_needs_a_new_one(self):
        self.work("README.md", "widgets, by 03\n", "Retitle the readme")
        self.commit("README.md", "widgets, by another\n", "Another ticket landed", cwd=self.other_checkout())
        self.prompt("lgtm")
        self.now += 120
        with self.assertRaises(subprocess.CalledProcessError):
            self.git("merge", "widgets-v2", cwd=self.worktree)
        (self.worktree / "README.md").write_text("widgets, by 03 and another\n")
        self.git("commit", "-q", "-am", "Merge widgets-v2", cwd=self.worktree, at=self.now - 60)

        self.assertRefused("no approval since")


    def test_a_land_that_loses_the_race_to_move_the_target_changes_nothing(self):
        self.work()
        self.prompt("lgtm")
        theirs = self.git("commit-tree", "widgets-v2^{tree}", "-p", "widgets-v2", "-m", "Another ticket landed")

        def another_worker_lands_first(cmd):
            if "update-ref" in cmd and self.tip("widgets-v2") != theirs:
                self.git("update-ref", "refs/heads/widgets-v2", theirs)

        self.run_fake.responses[("git", "before")] = another_worker_lands_first
        status = self.status()
        code, out = self.land()

        self.assertEqual(code, 1, out)
        self.assertIn("target moved; merge it, re-run pre-PR, land again", out)
        self.assertEqual(self.tip("widgets-v2"), theirs)
        self.assertEqual(self.status(), status)


    def test_a_target_checked_out_in_a_worktree_with_uncommitted_changes_is_refused_naming_it(self):
        checkout = self.other_checkout()
        (checkout / "README.md").write_text("widgets, half edited\n")
        self.work()
        self.prompt("lgtm")

        self.assertRefused(f"widgets-v2 is checked out in {checkout} with uncommitted changes")
        self.assertEqual((checkout / "README.md").read_text(), "widgets, half edited\n")


    def test_a_branch_already_landed_is_refused(self):
        self.work()
        self.prompt("lgtm")
        self.land()

        self.assertRefused("already landed")


    def test_a_clean_merge_of_anything_but_the_target_after_approval_needs_a_new_one(self):
        self.work()
        # Older than the approval, on main, checked out in the clone: none of it the target's.
        self.commit("base.py", "unreviewed = 1\n", "Unreviewed work on base", at=self.now - 600)
        self.prompt("lgtm")
        self.now += 120
        self.git("merge", "-q", "--no-edit", "main", cwd=self.worktree, at=self.now - 60)

        self.assertRefused("no approval since")


class LandedCleanupTest(LandTestCase):
    def setUp(self):
        super().setUp()
        self.work()
        self.prompt("lgtm")
        code, out = self.land()
        self.assertEqual(code, 0, out)
        self.shell("zsh")

    def shell(self, command):
        """The worker's shell pane runs `command`."""
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%6")] = command + "\n"

    def test_a_landed_worker_is_cleaned_up_its_ticket_done_and_its_unpushed_branch_deleted(self):
        self.afk("tick")

        self.assertFalse((self.project_dir / "workers" / "03").exists())
        self.assertFalse(self.worktree.exists())
        self.assertEqual(self.git("branch", "--list", "afk/widgets-03"), "")
        self.assertEqual(afk.parse_ticket(self.scratch / "issues" / "03-ui.md").status, "done")
        self.assertIn("03 landed and cleaned up", " ".join(c[-1] for c in self.run_fake.find("notify-send")))


    def test_a_landed_worker_with_a_busy_shell_waits_for_it_to_go_idle(self):
        self.shell("vim")

        self.afk("tick")

        self.assertEqual((self.status()["state"], self.status()["message"]), ("cleanup-pending", "its shell pane is running vim"))
        self.shell("zsh")
        self.afk("tick")
        self.assertFalse((self.project_dir / "workers" / "03").exists())

    def test_a_landed_worker_with_uncommitted_changes_waits_for_them(self):
        (self.worktree / "widget.py").write_text("widget = 2\n")

        self.afk("tick")

        self.assertEqual(self.status()["state"], "cleanup-pending")
        self.assertTrue(self.worktree.exists())


class HitlLandTest(LandTestCase):
    """A hitl ticket's guide-mode worker, landing on the human's request."""

    TYPE = "hitl"

    def test_the_humans_request_to_land_is_the_approval(self):
        self.assertEqual(self.status()["phase"], "hitl")
        self.work()
        before = self.tip("widgets-v2")
        self.prompt("land it")

        code, out = self.land()

        self.assertEqual(code, 0, out)
        landed = self.tip("widgets-v2")
        self.assertEqual(self.git("rev-parse", f"{landed}^"), before)
        self.assertEqual((self.status()["ended"], self.status()["landed_sha"]), ("landed", landed))

    def test_the_guide_mode_prompt_afk_launches_it_with_is_not_the_humans_approval(self):
        launch = shlex.split(self.run_fake.find("tmux", "send-keys")[-1][4])[-1]
        self.assertTrue(launch.startswith("AFK guide mode"), launch)
        self.work()
        self.prompt(launch)
        self.prompt("/tdd " + launch)  # as launched when the repo gives hitl a skill

        code, out = self.land()

        self.assertEqual(code, 1, out)
        self.assertIn("no approval since the branch's last commit", out)

    def test_a_commit_made_after_the_request_needs_the_human_to_confirm(self):
        self.prompt("land it")
        self.commit("widget.py", "widget = 1\n", "Add the widget", cwd=self.worktree, at=self.now)
        before = self.tip("widgets-v2")

        code, out = self.land()

        self.assertEqual(code, 1, out)
        self.assertIn("no approval since the branch's last commit", out)
        self.assertEqual(self.tip("widgets-v2"), before)

        self.now += 60
        self.prompt("yes, land it")
        code, out = self.land()

        self.assertEqual(code, 0, out)
        self.assertNotEqual(self.tip("widgets-v2"), before)

    def test_a_landed_hitl_worker_is_cleaned_up_like_any_landed_worker(self):
        self.work()
        self.prompt("land it")
        self.assertEqual(self.land()[0], 0)
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%6")] = "zsh\n"

        self.afk("tick")

        self.assertFalse((self.project_dir / "workers" / "03").exists())
        self.assertFalse(self.worktree.exists())
        self.assertEqual(self.git("branch", "--list", "afk/widgets-03"), "")
        self.assertEqual(afk.parse_ticket(self.scratch / "issues" / "03-ui.md").status, "done")
        self.assertIn("03 landed and cleaned up", " ".join(c[-1] for c in self.run_fake.find("notify-send")))


class LandPrModeTest(LandTestCase):
    INIT = ()

    def test_a_worker_landing_by_pr_is_refused(self):
        self.work()
        self.prompt("lgtm")
        before = self.tip("main")

        code, out = self.land()

        self.assertEqual(code, 1, out)
        self.assertIn("lands by PR", out)
        self.assertEqual(self.tip("main"), before)
        self.assertNotIn("ended", self.status())


class LandOutsideAWorkerTest(AfkTestCase):
    def test_land_outside_a_worker_says_to_run_it_from_one(self):
        self.run_fake.responses[("tmux", "show-options")] = ""  # a session with no project

        out = io.StringIO()
        code = afk.main(["land", "feat: x"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out,
                        clock=lambda: self.now)

        self.assertEqual(code, 1, out.getvalue())
        self.assertIn("afk: AFK_TICKET is not set; run this from an afk worker", out.getvalue())


class DropTestCase(LandTestCase):
    """Worker 03 of a local-mode project, as in LandTestCase, with an idle shell pane, for the human to drop."""

    def setUp(self):
        super().setUp()
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%6")] = "zsh\n"  # an idle shell pane

    def drop(self, *args):
        """`afk drop` run by the human from the project's session, as (exit code, output)."""
        out = io.StringIO()
        code = afk.main(["drop", *args], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out,
                        clock=lambda: self.now)
        return code, out.getvalue()

    def assertRefused(self, message, *args):
        """afk drop fails with `message`, leaving the worker, its worktree and its branch as they were."""
        status, head = self.status(), self.tip("HEAD", cwd=self.worktree)
        code, out = self.drop(*args)
        self.assertEqual(code, 1, out)
        self.assertIn(message, out)
        self.assertEqual(self.status(), status)
        self.assertEqual(self.tip("afk/widgets-03"), head)
        self.assertEqual(self.run_fake.find("tmux", "kill-window"), [])


class DropTest(DropTestCase):
    def test_a_dropped_worker_is_cleaned_up_with_its_unlanded_commits_and_its_ticket_offered_again(self):
        self.work()
        self.work("widget_test.py", "assert widget\n", "Test the widget")
        before = self.tip("widgets-v2")

        code, out = self.drop("03", "--discard")

        self.assertEqual(code, 0, out)
        self.assertIn("deleted 2 unlanded commits", out)
        self.assertEqual(self.run_fake.find("tmux", "kill-window"), [["tmux", "kill-window", "-t", "%5"]])
        self.assertFalse(self.worktree.exists())
        self.assertEqual(self.git("branch", "--list", "afk/widgets-03"), "")
        self.assertFalse((self.project_dir / "workers" / "03").exists())  # its slot is free
        self.assertEqual(self.tip("widgets-v2"), before)
        self.assertEqual(afk.parse_ticket(self.scratch / "issues" / "03-ui.md").status, "ready-for-agent")
        self.assertEqual([t["id"] for t in json.loads(self.afk("next"))["tickets"]], ["03"])

    def test_without_discard_it_refuses_saying_how_many_unlanded_commits_would_be_lost(self):
        self.work()
        self.work("widget_test.py", "assert widget\n", "Test the widget")

        self.assertRefused("dropping 03 would delete 2 unlanded commits; run `afk drop 03 --discard`", "03")

    def test_a_ticket_without_a_worker_is_refused(self):
        self.ticket("04-api", "Widget API")

        for ticket in ("04", "99"):
            code, out = self.drop(ticket, "--discard")

            self.assertEqual(code, 1, out)
            self.assertIn(f"afk: no worker for ticket {ticket}", out)
        self.assertEqual(self.run_fake.find("tmux", "kill-window"), [])

    def test_a_project_whose_state_is_gone_has_no_worker_to_drop(self):
        shutil.rmtree(self.project_dir)  # closed, though the session still names it

        code, out = self.drop("03", "--discard")

        self.assertEqual(code, 1, out)
        self.assertIn("afk: no worker for ticket 03", out)
        self.assertEqual(self.run_fake.find("tmux", "kill-window"), [])

    def test_without_discard_a_busy_shell_pane_or_uncommitted_changes_are_refused_too(self):
        self.work()
        (self.worktree / "widget.py").write_text("widget = 2\n")
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%6")] = "vim\n"

        self.assertRefused("dropping 03 would delete 1 unlanded commit and its worktree's uncommitted changes, and "
                           "end vim in its shell pane; run `afk drop 03 --discard`", "03")
        self.assertEqual((self.worktree / "widget.py").read_text(), "widget = 2\n")

    def test_with_discard_uncommitted_changes_are_deleted_too_and_it_says_so(self):
        (self.worktree / "widget.py").write_text("widget = 2\n")
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%6")] = "vim\n"

        code, out = self.drop("03", "--discard")

        self.assertEqual(code, 0, out)
        self.assertIn("deleted 0 unlanded commits and its worktree's uncommitted changes, and ended vim in its shell "
                      "pane", out)
        self.assertFalse(self.worktree.exists())
        self.assertFalse((self.project_dir / "workers" / "03").exists())

    def close_ticket_in_review(self):
        for _ in range(4):
            self.finish_phase()
        path = self.scratch / "issues" / "03-ui.md"
        path.write_text(path.read_text().replace("**Status:** ready-for-agent", "**Status:** closed"))
        self.now += afk.PR_POLL_SECONDS
        self.afk("tick")
        return path

    def test_a_worker_whose_ticket_is_closed_waits_on_its_unlanded_commits_until_it_is_dropped(self):
        self.work()

        path = self.close_ticket_in_review()

        message = "its branch has unpushed commits; `afk drop 03 --discard` deletes them"
        self.assertEqual((self.status()["state"], self.status()["message"]), ("cleanup-pending", message))
        self.assertIn(message, self.desktop_notifications()[-1])
        self.assertTrue(self.worktree.exists())

        code, out = self.drop("03", "--discard")

        self.assertEqual(code, 0, out)
        self.assertFalse((self.project_dir / "workers" / "03").exists())
        self.assertIn("**Status:** closed", path.read_text())  # left as it is

    def test_a_landed_worker_waiting_on_its_cleanup_is_refused_as_its_work_is_in_the_target(self):
        self.work()
        self.prompt("lgtm")
        self.assertEqual(self.land()[0], 0)

        self.assertRefused("03 has landed on widgets-v2", "03", "--discard")
        self.assertEqual(afk.parse_ticket(self.scratch / "issues" / "03-ui.md").status, "ready-for-agent")

    def test_a_worker_whose_worktree_is_gone_is_still_dropped(self):
        self.work()
        shutil.rmtree(self.worktree)

        code, out = self.drop("03", "--discard")

        self.assertEqual(code, 0, out)
        self.assertIn("deleted 1 unlanded commit;", out)
        self.assertEqual(self.git("branch", "--list", "afk/widgets-03"), "")
        self.assertFalse((self.project_dir / "workers" / "03").exists())

    def test_a_worker_whose_target_is_gone_waits_on_its_commits_and_is_still_dropped(self):
        self.work()
        self.git("branch", "-D", "widgets-v2")

        self.close_ticket_in_review()  # the tick doesn't fail

        self.assertEqual(self.status()["state"], "cleanup-pending")
        code, out = self.drop("03", "--discard")
        self.assertEqual(code, 0, out)
        self.assertIn("deleted 1 unlanded commit", out)

    def test_misplaced_or_unknown_options_are_refused_with_usage(self):
        for args in (("03", "--discard", "--force"), ("--discard", "03")):
            self.assertRefused("afk: usage: afk drop <ticket> [--discard]", *args)

    def test_a_worker_is_refused_dropping_a_ticket(self):
        status = self.status()
        out = io.StringIO()
        code = afk.main(["drop", "03", "--discard"], run=self.run_fake, env={**self.env, **self.worker_env()},
                        stdin=io.StringIO(), stdout=out)

        self.assertEqual(code, 1, out.getvalue())
        self.assertIn("afk drop is the human's", out.getvalue())
        self.assertEqual(self.status(), status)

    def test_a_worker_whose_ticket_is_closed_holding_only_the_targets_work_is_cleaned_up(self):
        self.commit("other.py", "other = 1\n", "Another ticket landed", cwd=self.other_checkout())
        self.git("merge", "-q", "widgets-v2", cwd=self.worktree)  # landed, so in the target, though never pushed

        self.close_ticket_in_review()

        self.assertFalse((self.project_dir / "workers" / "03").exists())


class DropPrModeTest(DropTestCase):
    INIT = ()

    def test_a_worker_landing_by_pr_is_refused_as_closing_its_pr_drops_it(self):
        self.work()

        self.assertRefused("03 lands by PR: close its PR instead", "03", "--discard")


class LinearWorkerTest(LinearTestCase):
    """Linear ticket ENG-2's worker, landing by PR, as its PR ends; ENG-3 is someone else's, so the project stays open.
    ENG-2 is a hitl ticket, so its PR is polled from the start."""

    def setUp(self):
        super().setUp()
        self.issue("ENG-2", "Widget API", description="Type: hitl\n")
        self.issue("ENG-3", "Widget UI", assignee="Someone Else")
        self.afk("init", self.SPEC)
        self.run_fake.responses[("tmux", "split-window")] = "%6\n"
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%6")] = "bash\n"  # its idle shell pane
        self.afk("start", "ENG-2")

    def updates(self):
        """The issue updates afk made after claiming ENG-2."""
        return self.run_fake.find("linear", "issue", "update")[1:]

    def test_a_merged_ticket_is_marked_done(self):
        self.run_fake.responses[("gh", "pr", "view")] = pr_view("MERGED")

        self.afk("tick")

        self.assertEqual(self.updates(), [["linear", "issue", "update", "ENG-2", "--state", "completed", "--no-input"]])
        self.assertEqual(self.run_fake.find("linear", "issue", "comment"), [])
        self.assertFalse((self.project_dir / "workers" / "ENG-2").exists())

    def test_a_ticket_whose_pr_closed_unmerged_is_unassigned_back_onto_the_frontier(self):
        self.run_fake.responses[("gh", "pr", "view")] = pr_view("CLOSED")

        self.afk("tick")

        self.assertEqual(self.updates(), [["linear", "issue", "update", "ENG-2", "--unassign", "--no-input"]])
        self.assertEqual(self.afk("frontier").splitlines(), ["ENG-2  Widget API"])

    def test_the_project_closes_once_its_last_ticket_merges(self):
        self.issues["ENG-3"]["state"] = "completed"
        self.run_fake.responses[("gh", "pr", "view")] = pr_view("MERGED")

        self.afk("tick")

        self.assertFalse(self.project_dir.exists())
        [entry] = (self.state_home / "afk" / "closed.log").read_text().splitlines()
        self.assertEqual(entry.split("\t")[1:], ["eng1", self.SPEC, "2 tickets done"])


class LinearCloseTest(LinearTestCase):
    def test_close_refuses_while_a_ticket_is_open_and_closes_once_every_ticket_has_ended(self):
        self.issue("ENG-2", "Widget API", state="completed")
        self.issue("ENG-3", "Widget UI")
        self.afk("init", self.SPEC)
        out = io.StringIO()

        code = afk.main(["close"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out, clock=lambda: self.now)

        self.assertEqual(code, 1)
        self.assertIn("tickets ENG-3 aren't done", out.getvalue())
        self.issues["ENG-3"]["state"] = "canceled"
        self.assertIn("project eng1 finished: 2 tickets done", self.afk("close"))
        self.assertFalse(self.project_dir.exists())


class LinearDropTest(LinearTestCase):
    def test_a_dropped_workers_ticket_is_unassigned_back_onto_the_frontier(self):
        self.issue("ENG-2", "Widget API")
        self.afk("init", self.SPEC, "--land", "local")
        self.run_fake.responses[("tmux", "split-window")] = "%6\n"
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%6")] = "bash\n"
        self.afk("start", "ENG-2")
        self.run_fake.responses[("git", "-C", str(self.repo), "rev-list")] = "1\n"

        self.afk("drop", "ENG-2", "--discard")

        self.assertEqual(self.run_fake.find("linear", "issue", "update")[1:],
                         [["linear", "issue", "update", "ENG-2", "--unassign", "--no-input"]])
        self.assertEqual(self.afk("frontier").splitlines(), ["ENG-2  Widget API"])


class LinearLandTest(RealGit, LinearTestCase):
    """Linear ticket ENG-2's local-mode worker, in a real git clone, landing on base."""

    def setUp(self):
        super().setUp()
        self.issue("ENG-2", "Widget API")
        self.issue("ENG-3", "Widget UI", assignee="Someone Else")
        self.afk("init", self.SPEC, "--land", "local")
        self.real_git()
        self.run_fake.responses[("tmux", "split-window")] = "%6\n"
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%6")] = "zsh\n"
        self.afk("start", "ENG-2")

    def test_a_landed_ticket_is_marked_done_with_a_comment_naming_where_it_landed(self):
        env = {"AFK_PROJECT": "eng1", "AFK_TICKET": "ENG-2"}
        worktree = self.project_dir / "worktrees" / "ENG-2"
        self.commit("widget.py", "widget = 1\n", "Add the widget", cwd=worktree, at=self.now - 60)
        self.afk("hook", "prompt", stdin=json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "lgtm"}), env=env)
        self.afk("land", "feat(widgets): add the widget\n\nTicket: ENG-2\n", env=env)
        sha = self.git("rev-parse", "main")

        self.afk("tick")

        self.assertEqual(self.run_fake.find("linear", "issue", "update")[1:],
                         [["linear", "issue", "update", "ENG-2", "--state", "completed", "--no-input"]])
        self.assertEqual(self.run_fake.find("linear", "issue", "comment"),
                         [["linear", "issue", "comment", "add", "ENG-2", "--body", f"Landed in {sha} on main", "--yes", "--no-input"]])
        self.assertFalse((self.project_dir / "workers" / "ENG-2").exists())


class LimitsTest(unittest.TestCase):
    """Runaway limits, one pure tick() at a time with an injected clock."""

    limits = {"phase_minutes": 60, "idle_minutes": 15, "fix_loops": 2}

    def worker(self, **changes):
        return {"ticket": "03", "phase": "implement", "state": "working", "message": "", "pane": "%5",
                "window": "03-widget-ui", "shown": "working", "phase_started_at": 0.0, **changes}

    def test_a_worker_working_past_the_phase_limit_is_interrupted_marked_stuck_and_notified(self):
        status, effects = afk.tick("03", self.worker(), 61 * 60, limits=self.limits, last_activity=61 * 60)

        self.assertEqual(status["state"], "stuck")
        self.assertIn(("interrupt", "%5"), effects)
        [(_, notification)] = [e for e in effects if e[0] == "notify"]
        self.assertIn("03", notification)
        self.assertIn("stuck", notification)
        self.assertIn("60m", notification)

    def test_a_worker_within_the_phase_limit_keeps_working(self):
        status, effects = afk.tick("03", self.worker(), 59 * 60, limits=self.limits, last_activity=59 * 60)

        self.assertEqual(status["state"], "working")
        self.assertEqual(effects, [])


    def test_a_worker_with_no_activity_past_the_idle_limit_is_interrupted_and_stuck(self):
        status, effects = afk.tick("03", self.worker(), 30 * 60, limits=self.limits, last_activity=14 * 60)

        self.assertEqual(status["state"], "stuck")
        self.assertIn("idle", status["message"])
        self.assertIn(("interrupt", "%5"), effects)
        self.assertEqual(len([e for e in effects if e[0] == "notify"]), 1)

    def test_a_stopped_worker_with_no_activity_past_the_idle_limit_needs_attention_without_an_interrupt(self):
        status, effects = afk.tick("03", self.worker(idle=True), 30 * 60, limits=self.limits, last_activity=14 * 60)

        self.assertEqual((status["state"], status["message"]), ("attention", "stopped without reporting"))
        self.assertNotIn(("interrupt", "%5"), effects)
        self.assertEqual(len([e for e in effects if e[0] == "notify"]), 1)

    def test_a_stopped_worker_past_the_phase_limit_is_not_interrupted_while_within_the_idle_limit(self):
        status, effects = afk.tick("03", self.worker(idle=True), 61 * 60, limits=self.limits, last_activity=59 * 60)

        self.assertEqual((status["state"], effects), ("working", []))

    def test_recent_activity_keeps_the_worker_working(self):
        status, effects = afk.tick("03", self.worker(), 30 * 60, limits=self.limits, last_activity=16 * 60)

        self.assertEqual((status["state"], effects), ("working", []))

    def test_a_worker_that_reported_done_but_keeps_running_is_still_held_to_the_limits(self):
        status, effects = afk.tick(
            "03", self.worker(state="done", idle=False), 61 * 60, limits=self.limits, last_activity=61 * 60
        )

        self.assertEqual(status["state"], "stuck")
        self.assertIn(("interrupt", "%5"), effects)

    def test_a_review_round_reported_before_its_stop_is_not_held_to_the_phase_clock(self):
        worker = self.worker(phase="review", state="done", idle=False, shown="review")
        status, effects = afk.tick("03", worker, 600 * 60, limits=self.limits, last_activity=600 * 60)

        self.assertEqual((status["state"], effects), ("done", [("window", "%5", "03-widget-ui", "done")]))

    def review_round(self, status, now):
        """The human gives feedback in the pane; the worker fixes it, reports done and stops."""
        return afk.tick("03", {**status, "state": "done", "message": "Fixed", "idle": True}, now, limits=self.limits)

    def test_review_rounds_past_the_fix_loop_limit_are_marked_stuck_and_notified(self):
        status = self.worker(phase="review", state="review", shown="review")
        for round in range(2):
            status, _ = self.review_round(status, now=round)
            self.assertEqual(status["state"], "review")

        status, effects = self.review_round(status, now=3)

        self.assertEqual(status["state"], "stuck")
        self.assertIn("2", status["message"])
        [(_, notification)] = [e for e in effects if e[0] == "notify"]
        self.assertIn("stuck", notification)
        self.assertFalse([e for e in effects if e[0] == "prompt"])

    def test_a_worker_stuck_on_fix_loops_returns_to_review_after_the_humans_next_round(self):
        status = self.worker(phase="review", state="review", shown="review")
        for round in range(3):
            status, _ = self.review_round(status, now=round)
        self.assertEqual(status["state"], "stuck")

        status, _ = self.review_round(status, now=4)

        self.assertEqual(status["state"], "review")

    def critique_round(self, status, now, message="round: fixed a finding"):
        """The worker finishes a critique round, reports done and stops."""
        return afk.tick("03", {**status, "state": "done", "message": message, "idle": True}, now,
                        limits={**self.limits, "critique_rounds": 2})

    def test_critique_rounds_past_the_limit_are_marked_stuck_and_notified(self):
        status, effects = self.critique_round(self.worker(phase="critique", landing="local"), now=1)
        self.assertEqual((status["phase"], status["state"]), ("critique", "working"))
        self.assertIn(("prompt", "03", "%5", "critique"), effects)

        status, effects = self.critique_round(status, now=3)

        self.assertEqual((status["phase"], status["state"]), ("critique", "stuck"))
        self.assertIn("still fixing after 2 critique rounds", status["message"])
        [(_, notification)] = [e for e in effects if e[0] == "notify"]
        self.assertIn("stuck", notification)
        self.assertFalse([e for e in effects if e[0] in ("prompt", "interrupt")])

    def test_a_worker_stuck_on_critique_rounds_gets_more_rounds_after_the_human_looks(self):
        status = self.worker(phase="critique", landing="local")
        for round in range(2):
            status, _ = self.critique_round(status, now=round)
        self.assertEqual(status["state"], "stuck")

        status, effects = self.critique_round({**status, "state": "working"}, now=4)

        self.assertEqual((status["phase"], status["state"]), ("critique", "working"))
        self.assertIn(("prompt", "03", "%5", "critique"), effects)

    def test_a_critique_round_that_finds_nothing_to_fix_moves_on_to_review(self):
        status, effects = self.critique_round(self.worker(phase="critique", landing="local"), now=1, message="Clean")

        self.assertEqual((status["phase"], status["state"]), ("review", "review"))
        self.assertIn(("prompt", "03", "%5", "review"), effects)


class NextTest(AfkTestCase):
    """`afk next` gives the /afk next skill what it needs to propose a batch: capacity, workers and candidates."""

    def setUp(self):
        super().setUp()
        self.ticket("03-ui", "Widget UI", type="frontend")
        self.ticket("05-export", "Widget export")
        self.ticket("07-import", "Widget import", blocked_by="05")
        self.afk("init", str(self.scratch))
        self.repo_config(TASK_TYPES)
        (self.repo / "docs" / "agents" / "prompts").mkdir()
        (self.repo / "docs" / "agents" / "prompts" / "frontend.md").write_text("Match the design.\n")

    def next(self):
        return json.loads(self.afk("next"))

    def test_next_lists_unblocked_tickets_with_their_resolved_task_type(self):
        proposal = self.next()

        self.assertEqual(
            proposal["tickets"],
            [
                {"id": "03", "title": "Widget UI", "path": str(self.scratch / "issues" / "03-ui.md"), "repo": None, "clone": str(self.repo),
                 "type": "frontend", "agent": "claude", "model": "sonnet", "effort": "medium"},
                {"id": "05", "title": "Widget export", "path": str(self.scratch / "issues" / "05-export.md"), "repo": None, "clone": str(self.repo),
                 "type": "backend", "agent": "claude", "model": "opus", "effort": "high"},
            ],
        )
        self.assertEqual(proposal["capacity"], 3)

    def test_running_workers_take_capacity_and_leave_the_candidates(self):
        with open(self.project_dir / "config.toml", "a") as config:
            config.write("\n[limits]\nmax_workers = 2\n")
        self.afk("start", "03")

        proposal = self.next()

        self.assertEqual(proposal["capacity"], 1)
        self.assertEqual([t["id"] for t in proposal["tickets"]], ["05"])
        self.assertEqual(
            proposal["workers"],
            [{"ticket": "03", "title": "Widget UI", "phase": "implement", "state": "working", "type": "frontend"}],
        )


class SplitTest(AfkTestCase):
    """A ticket spanning repos is split into one sibling ticket per repo, replacing it in the dependency graph."""

    def setUp(self):
        super().setUp()
        self.ticket("01-schema", "Widget schema", status="done")
        self.ticket("03-ui", "Widget UI")
        self.ticket("05-export", "Widget export", blocked_by="01")
        self.afk("init", str(self.scratch))

    def split(self, ticket, parts):
        return self.afk("split", ticket, stdin=json.dumps(parts))

    def test_split_replaces_the_ticket_with_one_ticket_per_repo_that_inherits_its_blockers(self):
        self.split("05", SPLIT_PARTS)

        self.assertEqual(
            self.afk("frontier").splitlines(),
            ["03  Widget UI", "06  Export endpoint  (acme/api)", "07  Export button  (acme/web)"],
        )
        self.assertIn("Serve widgets as CSV.", (self.scratch / "issues" / "06-export-endpoint.md").read_text())
        self.ticket("01-schema", "Widget schema")  # reopening the original's blocker blocks every part again
        self.assertEqual(self.afk("frontier").splitlines(), ["01  Widget schema", "03  Widget UI"])

    def test_split_into_no_parts_or_a_malformed_part_fails_before_writing_anything(self):
        malformed = [
            [],
            [SPLIT_PARTS[0], {"repo": "acme/web", "title": "Export button"}],
            [SPLIT_PARTS[0], "acme/web"],
            [{**SPLIT_PARTS[0], "title": "Export\n**Status:** done"}],
            [{**SPLIT_PARTS[0], "repo": "acme/api\nType: frontend"}],
        ]
        for parts in malformed:
            with self.subTest(parts=parts):
                out = io.StringIO()
                code = afk.main(["split", "05"], run=self.run_fake, env=self.env, stdin=io.StringIO(json.dumps(parts)), stdout=out)

                self.assertNotEqual(code, 0)
                self.assertIn("non-empty JSON list of {repo, title, body}", out.getvalue())
                self.assertEqual(self.afk("frontier").splitlines(), ["03  Widget UI", "05  Widget export"])

    def test_metadata_lines_in_a_parts_body_do_not_override_its_own(self):
        body = "Copied from 05.\n\n**Blocked by:** 999\n\n**Status:** done\n\n**Repo:** acme/other\n"
        self.split("05", [{"repo": "acme/api", "title": "Export endpoint", "body": body}])

        self.assertEqual(self.afk("frontier").splitlines(), ["03  Widget UI", "06  Export endpoint  (acme/api)"])

    def test_tickets_blocked_by_the_split_ticket_wait_for_every_part(self):
        self.ticket("04-report", "Widget report", blocked_by="03, 05")
        self.ticket("03-ui", "Widget UI", status="done")
        self.split("05", SPLIT_PARTS)

        self.ticket("06-export-endpoint", "Export endpoint", status="done")
        still_blocked = self.afk("frontier").splitlines()
        self.ticket("07-export-button", "Export button", status="done")
        unblocked = self.afk("frontier").splitlines()

        self.assertEqual(still_blocked, ["07  Export button  (acme/web)"])
        self.assertEqual(unblocked, ["04  Widget report"])


class UnblockedTest(AfkTestCase):
    """The watcher surfaces unblocked tickets and free capacity as a judgment point, for the user's /afk next."""

    def setUp(self):
        super().setUp()
        self.ticket("03-ui", "Widget UI")
        self.ticket("05-export", "Widget export")
        self.afk("init", str(self.scratch))

    def tick(self):
        calls_before = len(self.run_fake.calls)
        output = self.afk("tick")
        notifications = [" ".join(c[1:]) for c in self.run_fake.calls[calls_before:] if c[0] == "notify-send"]
        return output, notifications

    def test_unblocked_tickets_with_free_capacity_notify_once_and_show_on_the_dashboard(self):
        output, notifications = self.tick()

        self.assertEqual(notifications, ["afk: widgets 2 tickets unblocked — run /afk next"])
        self.assertIn("2 tickets unblocked — run /afk next", output.splitlines())
        self.assertNotIn("claude", [arg for c in self.run_fake.calls for arg in c])  # no LLM until the user asks

        self.now += 120
        output, notifications = self.tick()

        self.assertEqual(notifications, [])
        self.assertIn("2 tickets unblocked — run /afk next", output.splitlines())

    def test_a_newly_unblocked_ticket_notifies_again_once_the_tracker_is_next_polled(self):
        self.tick()
        self.ticket("07-import", "Widget import")

        self.now += 30
        _, early = self.tick()
        self.now += 31
        _, polled = self.tick()

        self.assertEqual(early, [])
        self.assertEqual(polled, ["afk: widgets 3 tickets unblocked — run /afk next"])

    def test_a_ticket_that_is_blocked_again_notifies_again_once_it_unblocks(self):
        self.tick()
        self.ticket("05-export", "Widget export", blocked_by="03")
        self.now += 60
        self.tick()
        self.ticket("05-export", "Widget export")

        self.now += 60
        _, notifications = self.tick()

        self.assertEqual(notifications, ["afk: widgets 2 tickets unblocked — run /afk next"])

    def test_no_judgment_point_while_every_worker_slot_is_taken(self):
        with open(self.project_dir / "config.toml", "a") as config:
            config.write("\n[limits]\nmax_workers = 1\n")
        self.afk("start", "03")

        output, notifications = self.tick()

        self.assertEqual(notifications, [])
        self.assertNotIn("unblocked", output)


class CloseTest(AfkTestCase):
    """`afk close` removes a project's state once nothing in it can lose the user's work."""

    def setUp(self):
        super().setUp()
        self.ticket("03-ui", "Widget UI")
        self.afk("init", str(self.scratch))

    def close(self, *args):
        out = io.StringIO()
        code = afk.main(["close", *args], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out,
                        clock=lambda: self.now)
        return code, out.getvalue()

    def notifications(self):
        return [" ".join(c[1:]) for c in self.run_fake.calls if c[0] == "notify-send"]

    def test_closing_a_finished_project_removes_its_state_unbinds_the_session_and_logs_it(self):
        self.ticket("03-ui", "Widget UI", status="done")

        code, out = self.close()

        self.assertEqual(code, 0, out)
        self.assertFalse(self.project_dir.exists())
        self.assertIn(["git", "-C", str(self.repo), "worktree", "prune"], self.run_fake.calls)
        self.assertIn(["tmux", "set-option", "-u", "-t", "work", "@afk_project"], self.run_fake.calls)
        self.assertEqual(self.notifications(), ["afk: widgets project widgets finished: 1 ticket done"])
        [entry] = (self.state_home / "afk" / "closed.log").read_text().splitlines()
        self.assertIn("widgets", entry)
        self.assertIn(str(self.scratch), entry)
        self.assertIn("1 ticket done", entry)

    def test_a_close_that_fails_keeps_the_session_bound_so_it_can_be_retried(self):
        self.ticket("03-ui", "Widget UI", status="done")
        (self.state_home / "afk" / "closed.log").mkdir()  # so the log can't be appended to

        with self.assertRaises(OSError):
            self.close()

        self.assertEqual(self.run_fake.find("tmux", "set-option", "-u"), [])
        self.assertTrue((self.project_dir / "config.toml").exists())

    def test_close_refuses_a_project_with_open_tickets(self):
        code, out = self.close()

        self.assertEqual(code, 1)
        self.assertIn("03", out)
        self.assertIn("--force", out)
        self.assertTrue(self.project_dir.exists())

    def test_force_closes_a_project_with_open_tickets(self):
        self.ticket("05-export", "Widget export", status="done")

        code, out = self.close("--force")

        self.assertEqual(code, 0, out)
        self.assertFalse(self.project_dir.exists())
        self.assertEqual(self.notifications(), ["afk: widgets project widgets closed: 1 ticket done, 1 open"])

    def test_close_refuses_while_a_worker_remains_even_when_forced(self):
        self.run_fake.responses[("tmux", "split-window")] = "%6\n"
        self.afk("start", "03")

        code, out = self.close("--force")

        self.assertEqual(code, 1)
        self.assertIn("03", out)
        self.assertTrue((self.project_dir / "workers" / "03").exists())

    def test_a_start_racing_a_close_cannot_start_a_worker_the_close_then_removes(self):
        self.ticket("05-export", "Widget export")
        self.run_fake.responses[("tmux", "split-window")] = "%6\n"
        codes = []

        def start_05():
            out = io.StringIO()
            codes.append(afk.main(["start", "05"], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out))

        other = threading.Thread(target=start_05)

        def start_arrives_mid_close(cmd):
            if cmd[3:5] == ["worktree", "prune"] and not other.is_alive() and not codes:
                other.start()
                other.join(timeout=0.2)  # without a lock it starts its worker now, after the close's checks
            return git(cmd)

        self.run_fake.responses[("git", "-C")] = start_arrives_mid_close
        code, out = self.close("--force")
        other.join()

        self.assertEqual(code, 0, out)
        self.assertEqual(codes, [1])
        self.assertFalse((self.project_dir / "config.toml").exists())

    def test_close_refuses_while_a_worktree_is_left_behind(self):
        self.ticket("03-ui", "Widget UI", status="done")
        (self.project_dir / "worktrees" / "03").mkdir(parents=True)

        code, out = self.close()

        self.assertEqual(code, 1)
        self.assertIn(str(self.project_dir / "worktrees" / "03"), out)
        self.assertTrue(self.project_dir.exists())

    def test_close_rejects_unknown_options(self):
        code, out = self.close("--now")

        self.assertEqual(code, 1)
        self.assertIn("usage: afk close [--force]", out)
class ProjectLandedTest(AfkTestCase):
    """A local-mode project landing on an integration branch tells the human once all its tickets have landed there."""

    LANDED = "all tickets landed on widgets-v2; open a PR to main when ready"

    def setUp(self):
        super().setUp()
        self.ticket("03-ui", "Widget UI", status="done")
        self.ticket("05-export", "Widget export", status="closed")

    def tick(self):
        calls_before = len(self.run_fake.calls)
        output = self.afk("tick")
        notifications = [" ".join(c[1:]) for c in self.run_fake.calls[calls_before:] if c[0] == "notify-send"]
        return output, notifications

    def test_every_ticket_landed_notifies_once(self):
        self.afk("init", str(self.scratch), "--land", "local", "--target", "widgets-v2")

        output, notifications = self.tick()

        self.assertEqual(notifications, [f"afk: widgets {self.LANDED}"])
        self.assertIn(self.LANDED, output.splitlines())
        self.now += 120
        _, later = self.tick()
        self.assertEqual(later, [])
        self.assertTrue((self.project_dir / "config.toml").exists())  # kept open until the human closes it

    def test_a_ticket_added_later_reopens_the_project_until_it_too_lands(self):
        self.afk("init", str(self.scratch), "--land", "local", "--target", "widgets-v2")
        self.tick()
        self.ticket("07-import", "Widget import")

        self.now += 60
        output, reopened = self.tick()
        self.ticket("07-import", "Widget import", status="done")
        self.now += 60
        _, landed = self.tick()

        self.assertNotIn(f"afk: widgets {self.LANDED}", reopened)
        self.assertNotIn(self.LANDED, output.splitlines())
        self.assertEqual(landed, [f"afk: widgets {self.LANDED}"])

    def test_no_notice_in_pr_mode_or_when_the_target_is_base_where_the_finished_project_closes(self):
        for init in [(), ("--land", "local"), ("--land", "local", "--target", "main")]:
            with self.subTest(init=init):
                shutil.rmtree(self.project_dir, ignore_errors=True)
                self.afk("init", str(self.scratch), *init)

                output, notifications = self.tick()

                self.assertNotIn("all tickets landed", output)
                self.assertEqual(notifications, ["afk: widgets project widgets finished: 2 tickets done"])
                self.assertFalse(self.project_dir.exists())

    def test_no_notice_while_a_worker_is_left(self):
        self.ticket("07-import", "Widget import")
        self.afk("init", str(self.scratch), "--land", "local", "--target", "widgets-v2")
        self.afk("start", "07")
        self.ticket("07-import", "Widget import", status="closed")  # closed by hand, its worker not yet cleaned up

        output, notifications = self.tick()

        self.assertNotIn("all tickets landed", output)
        self.assertEqual(notifications, [])
        self.assertNotIn("all tickets landed", self.afk("status"))

    def test_a_ticket_started_and_dropped_between_polls_does_not_count_as_landed(self):
        self.afk("init", str(self.scratch), "--land", "local", "--target", "widgets-v2")
        self.tick()
        self.ticket("07-import", "Widget import")
        self.afk("start", "07")
        self.now += 10
        self.tick()

        shutil.rmtree(self.project_dir / "workers" / "07")  # dropped, its ticket back on the frontier
        self.now += 10
        output, notifications = self.tick()

        self.assertNotIn("all tickets landed", output)
        self.assertNotIn(f"afk: widgets {self.LANDED}", notifications)

    def test_a_target_that_is_only_the_project_clones_base_is_still_an_integration_branch_in_another_clone(self):
        self.repo_config('base = "widgets-v2"')
        api = self.tmp / "api"
        (api / "docs" / "agents").mkdir(parents=True)
        (api / "docs" / "agents" / "afk.md").write_text('+++\nbase = "main"\n+++\n')
        self.afk("init", str(self.scratch), "--land", "local", "--target", "widgets-v2")
        with open(self.project_dir / "config.toml", "a") as config:
            config.write(f'\n[repos]\n"acme/api" = "{api}"\n')

        output, _ = self.tick()

        self.assertIn(self.LANDED, output.splitlines())

    def test_status_shows_the_notice_while_every_ticket_has_landed(self):
        self.afk("init", str(self.scratch), "--land", "local", "--target", "widgets-v2")

        landed = self.afk("status").splitlines()
        self.ticket("07-import", "Widget import")
        reopened = self.afk("status").splitlines()

        self.assertEqual(landed[-1], self.LANDED)
        self.assertNotIn(self.LANDED, reopened)


class ApprovedStartTest(AfkTestCase):
    """Starting the tickets approved at Gate 1: within max_workers, with the user's corrections."""

    def setUp(self):
        super().setUp()
        self.ticket("03-ui", "Widget UI")
        self.ticket("05-export", "Widget export")
        self.afk("init", str(self.scratch))
        self.repo_config(TASK_TYPES)
        (self.repo / "docs" / "agents" / "prompts").mkdir()
        (self.repo / "docs" / "agents" / "prompts" / "frontend.md").write_text("Match the design.\n")
        panes = iter(["%5", "%6"])
        self.run_fake.responses[("tmux", "new-window")] = lambda cmd: next(panes) + "\n"

    def limit_workers(self, n):
        with open(self.project_dir / "config.toml", "a") as config:
            config.write(f"\n[limits]\nmax_workers = {n}\n")

    def start_fails(self, *argv):
        calls_before = len(self.run_fake.calls)
        out = io.StringIO()
        code = afk.main(["start", *argv], run=self.run_fake, env=self.env, stdin=io.StringIO(), stdout=out)
        self.assertNotEqual(code, 0)
        self.assertEqual([c for c in self.run_fake.calls[calls_before:] if c[0] in ("git", "gh") or c[1] == "new-window"], [])
        self.assertFalse((self.project_dir / "workers" / argv[0]).exists())
        return out.getvalue()

    def test_start_beyond_max_workers_fails_before_claiming_or_creating_anything(self):
        self.limit_workers(1)
        self.afk("start", "03")

        out = self.start_fails("05")

        self.assertIn("max_workers", out)

    def test_corrections_override_the_task_types_model_and_effort_and_are_recorded(self):
        self.afk("start", "05", "--type", "frontend", "--model", "haiku", "--effort", "low")

        [send] = self.run_fake.find("tmux", "send-keys")
        claude = shlex.split(send[4])
        claude = claude[claude.index("claude") :]
        self.assertEqual(claude[claude.index("--model") + 1], "haiku")
        self.assertEqual(claude[claude.index("--effort") + 1], "low")
        self.assertIn("Match the design.", claude[-1])  # the rest of the frontend type still applies
        status = json.loads((self.project_dir / "workers" / "05" / "status.json").read_text())
        self.assertEqual(
            {k: status[k] for k in ("type", "agent", "model", "effort")},
            {"type": "frontend", "agent": "claude", "model": "haiku", "effort": "low"},
        )

    def test_an_unsupported_agent_correction_fails_before_creating_anything(self):
        self.assertIn("grok", self.start_fails("05", "--agent", "grok"))

    def add_api_repo(self):
        """A second clone in the project, with its own AFK config, mapped from owner/name in project config."""
        self.api = self.tmp / "api"
        (self.api / "docs" / "agents").mkdir(parents=True)
        (self.api / "docs" / "agents" / "afk.md").write_text('+++\nbase = "develop"\n+++\n')
        with open(self.project_dir / "config.toml", "a") as config:
            config.write(f'\n[repos]\n"acme/api" = "{self.api}"\n')

    def test_a_repo_correction_starts_the_worker_in_that_repos_clone_from_its_base(self):
        self.add_api_repo()

        self.afk("start", "05", "--repo", "acme/api")

        worktree = self.project_dir / "worktrees" / "05"
        self.assertEqual(
            self.run_fake.find("git", "-C"),
            [["git", "-C", str(self.api), "worktree", "add", "-b", "afk/widgets-05", str(worktree), "develop"]],
        )
        status = json.loads((self.project_dir / "workers" / "05" / "status.json").read_text())
        self.assertEqual(status["repo"], "acme/api")

    def test_phase_prompts_target_the_base_branch_of_the_workers_repo(self):
        self.add_api_repo()
        self.afk("start", "05", "--repo", "acme/api")
        worker = {"AFK_PROJECT": "widgets", "AFK_TICKET": "05"}

        for _ in ("implement", "verify"):
            self.afk("report", "done", "ok", env=worker)
            self.afk("hook", "stop", stdin="{}", env=worker)
            self.afk("tick")

        prepr = [stdin for cmd, stdin in zip(self.run_fake.calls, self.run_fake.inputs) if cmd[:2] == ["tmux", "load-buffer"]][-1]
        self.assertIn("origin/develop", prepr)
        self.assertNotIn("origin/main", prepr)

    def test_a_merged_worker_is_cleaned_up_in_its_own_repos_clone(self):
        self.add_api_repo()
        self.run_fake.responses[("tmux", "split-window")] = "%9\n"
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%9")] = "bash\n"
        self.run_fake.responses[("gh", "pr", "view")] = pr_view("MERGED")
        self.afk("start", "05", "--repo", "acme/api")
        worker = {"AFK_PROJECT": "widgets", "AFK_TICKET": "05"}
        for message in ["Implemented", "Verified", "Checks pass", "https://github.com/acme/api/pull/7"]:
            self.afk("report", "done", message, env=worker)
            self.afk("hook", "stop", stdin="{}", env=worker)
            self.afk("tick")

        self.now += 60
        self.afk("tick")

        worktree = str(self.project_dir / "worktrees" / "05")
        self.assertEqual(
            [c for c in self.run_fake.find("git", "-C") if c[3] in ("worktree", "branch") and c[4] != "add"],
            [["git", "-C", str(self.api), "worktree", "remove", "--force", worktree],
             ["git", "-C", str(self.api), "branch", "-D", "afk/widgets-05"]],
        )

    def test_a_repo_the_project_has_no_clone_for_fails_before_creating_anything(self):
        self.add_api_repo()

        out = self.start_fails("05", "--repo", "acme/mobile")

        self.assertIn("acme/mobile", out)
        self.assertIn("acme/api", out)

if __name__ == "__main__":
    unittest.main()
