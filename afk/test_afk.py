import io
import json
import os
import shlex
import subprocess
import tempfile
import threading
import tomllib
import unittest
from datetime import datetime, timezone
from pathlib import Path

import afk


class FakeRun:
    """Stands in for the run() seam: records every command and replays scripted stdout.

    Responses are keyed by a command prefix; the longest matching prefix wins.
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
        matches = [p for p in self.responses if tuple(cmd[: len(p)]) == p]
        if not matches:
            return ""
        response = self.responses[max(matches, key=len)]
        return response(cmd) if callable(response) else response

    def find(self, *prefix):
        return [c for c in self.calls if tuple(c[: len(prefix)]) == prefix]


def pr_view(state, url="https://github.com/acme/widgets/pull/42", ready=False, created=None):
    """What `gh pr view <branch> --json state,url,labels,createdAt` prints for a PR in `state`, labelled by /open-pr
    if ready and opened at clock time `created` (by default, well after any worker started)."""
    labels = [{"name": "Ready for Review"}] if ready else []
    opened = datetime.fromtimestamp(created if created is not None else 2_000_000_000, timezone.utc)
    return json.dumps({"state": state, "url": url, "labels": labels, "createdAt": opened.strftime("%Y-%m-%dT%H:%M:%SZ")})


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
                ("git", "-C"): "",
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


class InitReposTest(GithubTestCase):
    """init finds the clones of the repos the sub-issues live in, and re-running it refreshes the project."""

    def clone(self, path, remote):
        (path / ".git").mkdir(parents=True)
        self.run_fake.responses[("git", "-C", str(path), "remote", "-v")] = f"origin\t{remote} (fetch)\norigin\t{remote} (push)\n"
        return path

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
        self.assertEqual(self.run_fake.find("git", "-C", str(self.repo), "worktree", "remove"), [["git", "-C", str(self.repo), "worktree", "remove", worktree]])
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
                           ("Notification", "notification")]:
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


class WatchTest(AfkTestCase):
    def setUp(self):
        super().setUp()
        self.ticket("03-ui", "Widget UI")
        self.afk("init", str(self.scratch))
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
            elif cmd[:2] == ["tmux", "send-keys"] and cmd[cmd.index("-t") + 1] == pane:
                self.assertEqual(cmd[-1], "Enter")
        return prompts

    def phase(self, ticket="03"):
        return json.loads((self.project_dir / "workers" / ticket / "status.json").read_text())["phase"]

    def status(self, ticket="03"):
        return json.loads((self.project_dir / "workers" / ticket / "status.json").read_text())

    def finish_phase(self, message="Done"):
        self.report("done", message)
        self.stop()
        self.afk("tick")

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

    def desktop_notifications(self):
        return [" ".join(c[1:]) for c in self.run_fake.calls[self.calls_before :] if c[0] == "notify-send"]

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

    def limit(self, **limits):
        with open(self.project_dir / "config.toml", "a") as config:
            config.write("\n[limits]\n" + "".join(f"{k} = {v}\n" for k, v in limits.items()))

    def interrupts(self, pane="%5"):
        return [c for c in self.run_fake.find("tmux", "send-keys")[1:] if c[c.index("-t") + 1] == pane and c[-1] == "Escape"]

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

    def test_a_killed_agent_pane_needs_attention_instead_of_failing_every_tick(self):
        def pane_gone(cmd):
            raise subprocess.CalledProcessError(1, cmd, stderr="can't find pane: %5")

        self.run_fake.responses[("tmux", "display-message", "-p", "-t")] = pane_gone
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
        self.assertEqual(self.run_fake.cwds[self.run_fake.calls.index(poll)], str(self.project_dir / "worktrees" / "03"))
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
                [["git", "-C", str(self.repo), "worktree", "remove", worktree]],
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

    def test_merged_pr_with_uncommitted_changes_is_left_cleanup_pending_and_notifies(self):
        self.reach_review()
        self.merge(worktree_status=" M src/widget.py\n")

        self.afk("tick")

        self.assertEqual(self.cleanup_commands(), ([], [], []))
        self.assertEqual(self.status()["state"], "cleanup-pending")
        self.assertIn("uncommitted", self.desktop_notifications()[-1])

    def test_merged_pr_whose_shell_pane_is_gone_is_left_cleanup_pending(self):
        self.reach_review()
        self.merge()

        def pane_gone(cmd):
            raise subprocess.CalledProcessError(1, cmd, stderr="can't find pane: %6")

        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%6")] = pane_gone

        self.afk("tick")

        self.assertEqual(self.cleanup_commands(), ([], [], []))
        self.assertEqual(self.status()["state"], "cleanup-pending")
        self.assertIn("shell pane is gone", self.desktop_notifications()[-1])

    def test_cleanup_pending_worker_is_cleaned_up_once_the_shell_is_idle_again(self):
        self.reach_review()
        self.merge(shell="vim")
        self.afk("tick")
        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%6")] = "bash\n"

        self.afk("tick")

        self.assertEqual([len(c) for c in self.cleanup_commands()], [1, 1, 1])
        self.assertEqual(self.afk("status").splitlines()[1:], [])

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
        self.assertEqual(self.run_fake.cwds[self.run_fake.calls.index(poll)], worktree)
        self.assertEqual(self.status("04")["state"], "cleanup-pending")
        self.assertIn("04 merged; cleanup pending", self.desktop_notifications()[-1])

        self.run_fake.responses[("tmux", "display-message", "-p", "-t", "%8")] = "bash\n"
        self.afk("tick")

        self.assertEqual(self.run_fake.find("tmux", "kill-window"), [["tmux", "kill-window", "-t", "%7"]])
        self.assertEqual(self.run_fake.find("git", "-C", str(self.repo), "worktree", "remove"), [["git", "-C", str(self.repo), "worktree", "remove", worktree]])
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
        self.assertEqual(self.run_fake.find("git", "-C", str(self.repo), "worktree", "remove"), [["git", "-C", str(self.repo), "worktree", "remove", worktree]])
        self.assertEqual(self.run_fake.find("git", "-C", str(self.repo), "branch", "-D"), [["git", "-C", str(self.repo), "branch", "-D", "afk/widgets-04"]])
        self.assertFalse((self.project_dir / "workers" / "04").exists())
        self.assertIn("04 ticket closed and cleaned up", self.desktop_notifications()[-1])
        self.assertIn("**Status:** closed", path.read_text())  # the tracker already has it as closed


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
            [["git", "-C", str(self.api), "worktree", "remove", worktree],
             ["git", "-C", str(self.api), "branch", "-D", "afk/widgets-05"]],
        )

    def test_a_repo_the_project_has_no_clone_for_fails_before_creating_anything(self):
        self.add_api_repo()

        out = self.start_fails("05", "--repo", "acme/mobile")

        self.assertIn("acme/mobile", out)
        self.assertIn("acme/api", out)

if __name__ == "__main__":
    unittest.main()
