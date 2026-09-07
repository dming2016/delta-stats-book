#!/usr/bin/env python3
"""Focused checks for the in-app update controls and JavaScript flow."""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "web" / "index.html"
THEME = ROOT / "web" / "assets" / "theme-1.9.4.css"


class UpdateUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX.read_text(encoding="utf-8")
        cls.theme = THEME.read_text(encoding="utf-8")
        cls.update_source = cls.html.split("const UPDATE_POLL_INTERVAL", 1)[1].split(
            "window.setDesktopMaximized", 1
        )[0]

    def test_topbar_menu_about_and_update_dialog_expose_the_update_flow(self) -> None:
        for fragment in (
            'id="topbarUpdateButton"',
            'id="topbarUpdateLabel"',
            'id="topbarUpdateStatus"',
            'data-app-command="updates"',
            'id="menuUpdateStatus"',
            'id="aboutUpdateStatus"',
            'id="aboutUpdateButton"',
            'id="updateDialog"',
            'id="updateProgress"',
            'id="updateProgressText"',
            'id="deferUpdateButton"',
            'id="cancelUpdateButton"',
            'id="startUpdateButton"',
        ):
            self.assertIn(fragment, self.html)
        self.assertIn("检查更新", self.html)
        self.assertIn("稍后", self.html)
        self.assertIn("立即更新", self.html)
        self.assertIn("取消下载", self.html)

    def test_topbar_update_chip_keeps_other_tools_visible_and_only_shows_when_needed(self) -> None:
        self.assertLess(
            self.html.index('id="refreshButton"'),
            self.html.index('id="topbarUpdateButton"'),
        )
        self.assertLess(
            self.html.index('id="topbarUpdateButton"'),
            self.html.index('id="appMenuButton"'),
        )
        self.assertIn('id="topbarUpdateButton" type="button"', self.html)
        self.assertIn('aria-label="检查软件更新" hidden', self.html)
        self.assertIn(
            "$('topbarUpdateButton').addEventListener('click', openUpdateCenter);",
            self.html,
        )
        self.assertIn("function topbarUpdatePresentation(status, phase)", self.update_source)
        self.assertIn("function updateTopbarVisible(status, phase)", self.update_source)
        self.assertIn("function renderTopbarUpdate(status, phase)", self.update_source)
        self.assertIn("renderTopbarUpdate(status, phase);", self.update_source)
        self.assertIn("button.hidden = !visible;", self.update_source)
        self.assertIn("if (!visible) return;", self.update_source)
        self.assertIn(".topbar-update", self.html)
        self.assertIn(".desktop-window .topbar-update { max-width: 78px", self.html)
        self.assertIn(".desktop-window .sync-state { display: none; }", self.html)

    def test_topbar_update_labels_cover_available_and_in_progress_states(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not installed")
        checker = (
            "const fs=require('fs');"
            "const html=fs.readFileSync(process.argv[1],'utf8');"
            "const start=html.indexOf('function topbarUpdatePresentation');"
            "const end=html.indexOf('function renderTopbarUpdate',start);"
            "if(start<0||end<0)throw new Error('topbar helper is missing');"
            "const helper=html.slice(start,end);"
            "const helpers=new Function("
            "\"const UPDATE_PROGRESS_PHASES=new Set(['downloading','verifying','extracting']);\"+"
            "\"const updateIsAvailable=(status)=>Boolean(status&&(status.update_available||status.phase==='available'));\"+"
            "\"const updateIsReady=(status)=>status?.phase==='ready';\"+"
            "helper+'return {presentation:topbarUpdatePresentation,visible:updateTopbarVisible};'"
            ")();"
            "const presentation=helpers.presentation;const visible=helpers.visible;"
            "const cases=["
            "[{phase:'idle'},'idle','更新','',false],"
            "[{phase:'checking'},'checking','检查中…','',false],"
            "[{phase:'up_to_date',current:'1.7.3'},'up_to_date','更新','',false],"
            "[{phase:'available',latest:'1.7.3',update_available:true},'available','更新','v1.7.3',true],"
            "[{phase:'downloading',latest:'1.7.3',update_available:true,completed:42,total:100},'downloading','更新中','42%',true],"
            "[{phase:'ready',latest:'1.7.3',update_available:true},'ready','安装更新','v1.7.3',true],"
            "[{phase:'error'},'error','更新','重试',true]"
            "];"
            "for(const [status,phase,label,state,shown] of cases){const actual=presentation(status,phase);"
            "if(actual.label!==label||actual.status!==state||visible(status,phase)!==shown)"
            "throw new Error(JSON.stringify({phase,actual,shown:visible(status,phase)}));}"
        )
        result = subprocess.run(
            [node, "-e", checker, str(INDEX)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_update_actions_use_only_the_desktop_bridge(self) -> None:
        for method in (
            "check_for_update",
            "get_update_status",
            "start_update",
            "cancel_update",
            "restart_to_update",
        ):
            self.assertIn(f"callUpdateApi('{method}'", self.update_source)
        self.assertNotIn("syncState", self.update_source)
        self.assertNotIn("appUrl(", self.update_source)

    def test_manual_flag_and_duplicate_guards_are_explicit(self) -> None:
        self.assertIn("async function checkForUpdate(manual)", self.update_source)
        self.assertIn("callUpdateApi('check_for_update', isManual)", self.update_source)
        self.assertIn("if (state.updateCheckPending)", self.update_source)
        self.assertIn("if (state.updateActionPending)", self.update_source)
        self.assertIn("if (state.updatePollPending)", self.update_source)

    def test_download_progress_uses_backend_byte_counts(self) -> None:
        self.assertIn("source.completed", self.update_source)
        self.assertIn("source.total", self.update_source)
        self.assertIn("$('updateProgress').max = status.total", self.update_source)
        self.assertIn("$('updateProgress').value = Math.min(status.completed, status.total)", self.update_source)
        self.assertIn("formatUpdateBytes(status.completed)", self.update_source)
        self.assertIn("formatUpdateBytes(status.total)", self.update_source)

    def test_backend_status_field_names_are_normalized(self) -> None:
        self.assertIn("source.available", self.update_source)
        self.assertIn("source.current_version", self.update_source)
        self.assertIn("source.latest_version", self.update_source)
        self.assertIn("source.release_history", self.update_source)
        self.assertIn("source.releaseHistory", self.update_source)

    def test_auto_check_is_delayed_until_desktop_window_is_ready(self) -> None:
        initializer = self.html.split("async function initializeDesktopWindow()", 1)[1].split(
            "window.addEventListener('pywebviewready'", 1
        )[0]
        scheduler = self.update_source.split("function scheduleAutomaticUpdateCheck()", 1)[1]
        self.assertLess(
            initializer.index("desktopWindowInitialized = true"),
            initializer.index("scheduleAutomaticUpdateCheck()"),
        )
        self.assertIn("window.setTimeout", scheduler)
        self.assertIn("checkForUpdate(false)", scheduler)

    def test_auto_errors_are_quiet_and_modal_prompts_do_not_stack(self) -> None:
        self.assertIn("quiet: !isManual", self.update_source)
        self.assertIn("quietError", self.update_source)
        self.assertIn("dialog[open]:not(#updateDialog)", self.update_source)
        self.assertIn("state.updateDialogPending", self.update_source)

    def test_dismissed_or_checking_update_dialog_can_be_opened_again(self) -> None:
        self.assertIn("function resetUpdateDialogRequest()", self.update_source)
        self.assertIn("state.updateDialogRequestId", self.html)
        self.assertIn(
            "$('updateDialog').addEventListener('close', resetUpdateDialogRequest);",
            self.html,
        )
        self.assertIn("showUpdateDialog({replacePending: true});", self.update_source)
        self.assertIn(
            "$('menuUpdateCommand').disabled = updateUiLocked(status);",
            self.update_source,
        )
        self.assertIn(
            "aboutButton.disabled = updateUiLocked(status);",
            self.update_source,
        )
        self.assertNotIn(
            "$('menuUpdateCommand').disabled = state.updateCheckPending",
            self.update_source,
        )

    def test_release_notes_support_strings_and_lists_without_html_injection(self) -> None:
        for fragment in (
            'id="updateReleaseNotes"',
            'id="updateReleaseNotesList"',
            'id="aboutReleaseNotes"',
            'id="aboutReleaseNotesList"',
            "function normalizeUpdateReleaseNotes(value)",
            "function normalizeUpdateReleaseHistory(value)",
            "source.release_notes",
            "source.releaseNotes",
            "release_notes: releaseNotes",
            "release_history: releaseHistory",
            "function currentReleaseNotes()",
            "function updateReleaseNoteGroups(status)",
            "function updateReleaseNotesPresentation(status = state.updateStatus)",
            "function renderReleaseNotes(sectionId, listId, notes)",
            "function renderUpdateReleaseHistory(groups)",
            "list.replaceChildren();",
            "item.textContent = note;",
            "version.textContent = group.version.startsWith('v') ? group.version : `v${group.version}`;",
            "renderUpdateReleaseNotes(releaseNotes);",
            "renderAboutReleaseNotes();",
        ):
            self.assertIn(fragment, self.html)
        self.assertIn(".update-release-notes", self.html)
        self.assertIn(".about-release-notes", self.html)
        self.assertIn("max-height: 180px", self.html)
        self.assertIn("overflow-y: auto", self.html)

    def test_release_notes_choose_the_target_or_current_version(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not installed")
        checker = (
            "const fs=require('fs');"
            "const html=fs.readFileSync(process.argv[1],'utf8');"
            "const start=html.indexOf('function normalizeUpdatePhase');"
            "const end=html.indexOf('function renderReleaseNotes',start);"
            "if(start<0||end<0)throw new Error('release notes helpers are missing');"
            "const helper=html.slice(start,end);"
            "const helpers=new Function('state',helper+"
            "'return {normalizeStatus:normalizeUpdateStatus,presentation:updateReleaseNotesPresentation};'"
            ")({appInfo:{release_notes:['当前版本说明一','当前版本说明二']}});"
            "const target=helpers.presentation(helpers.normalizeStatus({"
            "phase:'available',update_available:true,latest:'1.7.5',release_notes:['目标版本说明'],"
            "release_history:[{version:'1.7.4',release_notes:['1.7.4 更新内容']},"
            "{version:'1.7.3',notes:['1.7.3 更新内容']}]}));"
            "const current=helpers.presentation({phase:'up_to_date',current:'1.7.4'});"
            "if(target.title!=='本次更新内容'||target.notes.join('|')!=='目标版本说明')"
            "throw new Error(JSON.stringify(target));"
            "if(target.groups.map((group)=>`${group.version}:${group.notes.join(',')}`).join('|')!=="
            "'1.7.5:目标版本说明|1.7.4:1.7.4 更新内容|1.7.3:1.7.3 更新内容')"
            "throw new Error(JSON.stringify(target));"
            "if(current.title!=='当前版本更新内容'||current.notes.join('|')!=='当前版本说明一|当前版本说明二')"
            "throw new Error(JSON.stringify(current));"
            "if(current.groups.length!==0)throw new Error(JSON.stringify(current));"
        )
        result = subprocess.run(
            [node, "-e", checker, str(INDEX)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_loading_app_info_rerenders_current_release_notes(self) -> None:
        loader = self.html.split("async function loadAppInfo()", 1)[1].split(
            "async function openDesktopDirectory", 1
        )[0]
        self.assertIn("renderUpdateStatus();", loader)
        self.assertLess(loader.index("state.appInfo = info;"), loader.index("renderUpdateStatus();"))

    def test_update_styles_cover_progress_and_narrow_dialogs(self) -> None:
        for selector in (
            ".about-update",
            ".update-dialog",
            ".update-version-line",
            ".update-progress progress",
            ".update-progress-meta",
            ".update-error",
            ".update-dialog-foot",
        ):
            self.assertIn(selector, self.theme)
        narrow = self.theme.split("@media (max-width: 720px)", 1)[1]
        self.assertIn(".update-dialog-foot", narrow)
        self.assertIn("grid-template-columns: 1fr 1fr", narrow)

    def test_inline_javascript_has_valid_syntax(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not installed")
        checker = (
            "const fs=require('fs');"
            "const html=fs.readFileSync(process.argv[1],'utf8');"
            "const scripts=[...html.matchAll(/<script(?:\\s[^>]*)?>([\\s\\S]*?)<\\/script>/gi)];"
            "for(const match of scripts){if(match[1].trim())new Function(match[1]);}"
        )
        result = subprocess.run(
            [node, "-e", checker, str(INDEX)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
