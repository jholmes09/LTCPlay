# Removing interface 1.0: the plan

Jeff's decision (2026-11): 2.0 is a cosmetic refresh of 1.0. The code under it stays and is updated, never replaced. The release waits until everything in 1.0 that matters is in 2.0. Then 1.0 is removed entirely.

Read from the code on bench-build at 5e602d8. 1.0 is `ltcplay/web/remote.html` (served at `/remote1`, and at `/remote` for a screen that chose the old screen). 2.0 is `ltcplay/web/remote2.html` (served at `/remote2`, and at `/remote` by default).

**Default switched (Jeff, 2026-11-02).** `/remote` (and the show network's `/`) serves 2.0 to every screen. Only a screen's own cookie `ltcplay_ui=1` keeps 1.0 there: 2.0's "Open the old screen (for Users, Audit, Network and the rest)" link (header "Old screen" on every tab, in full on the Schedule and Settings tabs) sets it through `/ui?v=1` and opens `/remote1`; 1.0's Try 2.0 (`/ui?v=2`) clears it and goes back to `/remote`. `/remote1` and `/remote2` are unchanged. The show machine's `ui_default` setting no longer decides; its Settings card is gone from 2.0, its route stays until R1.

## Already in both (nothing to port)

| Area | 1.0 | 2.0 |
|---|---|---|
| Start now, Hold, Resume, Stop show (double tap), Run, Run off | show screen, Programming | SHOW tab, mini strip on other tabs |
| Flame groups: hold to arm, Disarm, Disarm all, Arm all | show screen | SHOW tab |
| Lasers: arm chips, Disarm all lasers | show screen | SHOW tab |
| Hazers: on, off, Hazers off, levels | show screen, levels panel | SHOW tab, levels sheet |
| Alerts, Acknowledge, rig check failed with Continue anyway | show screen | SHOW tab alert line |
| Announcements: play, Stop sound, Held for announcement | show screen | footer |
| Weather (wind) | show screen | footer, details sheet |
| Stale banner, Stop not confirmed, disarm failed | every screen | every tab |
| Sign in, sign out, first sign-in new password | Users screen | header |
| Startup: engine checks, checklist tick and note, purge select and fire (Space), lock and unlock, laser tests, calibrate all | Startup page | DAY tab |
| Shutdown: checklist, wave flamer cleaning | Startup page | DAY tab |
| Fuel: tanks, refill, Refill all | Fuel page | DAY tab, Fuel step |
| Live flame off, cleaning Stop, timed purge Stop (from any screen) | banners | pinned line |

## In 2.0 only (nothing to port)

| Area | Where in 2.0 | Routes |
|---|---|---|
| Fuel pump (run time, On, Pump off) | SHOW tab | `fuel-pump` |
| Fireball timeline editor (per show file, FSEQ or Timeline, Save, Revert) | Schedule tab | `fireball-timeline`, `fireball-timeline-save` |
| Play-from cues editor | Settings tab | `play-from-cues`, `play-from-cues-save` |
| Default interface (1.0 or 2.0) | REMOVED from the Settings tab when the default switched; no page calls the route | `ui-default` (removed with 1.0) |
| Coach marks, quick card, help sheets | every tab | none |

The Fireball manual page's merge with the show is server side (fireball.py, fireball_cues.py). Only its page is missing from 2.0 (P8).

## Gaps: in 1.0, not in 2.0

Routes are under `/api/remote/` unless they start with `/api/`. A route 2.0 already calls with another body is named with that body. "Controls" counts the buttons, fields and lists the operator uses. Every port PR replaces the matching "Open in 1.0" link in 2.0 and adds 2.0 to the request-parity tables (`test_ui2_page_makes_only_1_0_presses`).

| PR | Work item | Controls | Routes 2.0 does not call yet | Depends on | Risk |
|---|---|---|---|---|---|
| 0 | Shared rules in their own file (`ltcplay/web/remote_rules.js`). THIS PR. | 0 | none | none | Low. Served pages byte for byte the same. Later edits to the file get two cold reviews. |
| P1 | DONE (2.0 Schedule tab, Programming cards). Run card: show file picker, Show or Programming mode, Run in Programming | 4 | `show-select`; `/api/start` with `on_lost: hold` | 0 | Medium. Run starts the show engine. Show machine only. |
| P2 | DONE (2.0 Schedule tab, Programming cards). Programming transport: Play from (timecode or cue), Back 10 s, Back 5 s, Ahead 5 s, Ahead 10 s, Pause, Continue, Set A, Set B, Loop, Follow timecode again; flames follow line and fault | 12 | `play-from`, `jump`, `pause`, `continue`, `mark-a`, `mark-b`, `loop`, `follow` | P1 | High. Flame cues follow Programming playback. Safety review. |
| P3 | DONE (2.0 Schedule tab). Schedule tab: tonight's shows, tap for notes, Run warning | 2 | none (status) | 0 | Low. Read only. |
| P4 | DONE (2.0 Schedule tab, show PC only). Schedule editor: time zone, season start and end, show nights, show length, gap before a show, late start allowed, Save, Reload | 11 | `schedule-file` (GET, POST) | P3 | High. Schedule rules (#196 reviews), saving during the night. |
| P5 | DONE (2.0 Users pane, opened from Settings and the signed-in sheet). Users: change own password, user list (role, reset, remove), add a person, clear sign-in lockouts | 9 | `my-password`, `users` (POST), `clear-lockouts` | 0 | Medium. Last admin rule, passwords. |
| P6 | DONE (2.0 Users pane, opened from Settings and the signed-in sheet). Who may do what (role matrix), Save | 2 | `role-matrix` | P5 | High. Decides who may arm. Safety review. |
| P7 | DONE (2.0 Users pane, opened from Settings and the signed-in sheet). Audit log: chain state, Check the chain, nightly upload settings, Send now, idle sign out, sign out after the last show | 11 | `audit-verify`, `audit-settings`, `audit-upload-now`, `idle-signout`, `last-show-signout` | P5 | Medium. Token field. |
| P8 | DONE (2.0 DAY, Fireball step). Fireball page: armed state, per channel hold pads, All off, Disarm Fireball, charge record, Tanks are empty | 6 plus one pad per channel | `fireball`, `fireball-beat`, `fireball-off`, `fireball-charge-empty` | 0 | High. Fires channels by hold and heartbeat. Safety review. |
| P9 | DONE (2.0 DAY, Fuel step). Fuel: recent refills, Undo (admins), fuel warning | 2 | `fuel-undo` | 0 | Low. |
| P10 | DONE (2.0 DAY, Purge step). Timed purge: presets, other seconds, hold to start, Stop purge, time left | 5 | `purge-timed` | 0 | High. Fires for a set time. Safety review. |
| P11 | DONE (2.0 DAY, Purge step, Map; the Arrange button is shown on the show PC only, and the engine's `layout` route needs the show_files permission from any device, as in 1.0). Machine map and Arrange mode: field map, Arrange, Save layout, Reset layout, Cancel, drag | 5 | `layout`; open beat `arranging: true` | P10 | High. Arming is blocked while arranging (2.0 has `ARRANGE = false` today). Safety review. |
| P12 | DONE (2.0 DAY, the checklist steps; the PDF and Export all are links with `download`, for an iPad's Safari 15). Checklists: Verify records, Download today's PDF, Export all | 3 | `checklist-verify`, `checklist-pdf`, `checklist-export` | 0 | Low. Downloads on iPad Safari. |
| P13 | DONE (2.0 Programming cards, Schedule tab). Show machine switches: arming from a screen (on, off), live flame programming On (with confirm), announcement output picker, Save, Test tone | 6 | `screen-arming`, `announce-outputs`, `announce-output`, `announce-test-tone`; `live-flame` with `on: true` (2.0 calls it only with `on: false`, its Live flame off) | 0 | High. Arming from a screen and live flame On. Safety review. |
| P14 | DONE (2.0 Schedule tab, show PC cards; Reload engine in its own Settings changed card). Show machine tools: show network address, Save, Restart the engine, settings changed on disk with Reload engine, Stream Deck operator, rig check card (Check the rig, items) | 8 | `network` (GET, POST), `reload-engine`, `/api/schedule/operator` (GET, POST); `preroll-check` from a card | 0 | Medium. Engine restart. |
| P15 | DONE (2.0 Schedule tab: Getting started at the top, the Show folder card on the show PC). Show folder and Getting started: folder path, Use this folder, Make a show file, Check the show file, open show and settings folders, the three setup steps | 7 | `show-folder`, `show-file-new`, `/api/check`, `open-folder`, `setup` | P1 | Medium. Fresh install flow. |
| P16 | DONE (2.0 Help, This LTC Player). Info panel: version, screen, settings path, mode, how to leave the window, Close this window | 2 | `screen` | 0 | Low. |
| R1 | Remove 1.0: `remote.html`, `/remote1`, `/ui`, the `ltcplay_ui` cookie, `ui-default` (its Settings card already went with the default switch), 2.0's old screen links (header "Old screen", the Schedule and Settings cards, every "Open in 1.0"), the 1.0 window title | 0 | removes routes | P1 to P16; Jeff's yes | High. About 80 selftest reads of 1.0, about 200 mutations on `remote.html`, the parity tables become fixed tables. |
| R2 | Docs: OPERATOR.md, USERS.md, MORNING.md, FUEL.md, TESTING.md say 2.0 only | 0 | none | R1 | Low. |

Totals: 16 port PRs (P1 to P16) with 95 or more controls, 42 routes 2.0 does not call yet, and 4 new bodies on routes it already calls (`/api/start`, the open beat, `preroll-check`, `live-flame`). With this PR and R1, R2: 19 PRs.

## Left before R1

With bundle E (P12, P14, P15, P16) and #214 (P5, P6, P7), every port is done: 2.0 calls every route 1.0 calls, and no 2.0 card or sheet opens a part of 1.0. What is left before R1 deletes 1.0:

| Item | What is left |
|---|---|
| 2.0's old screen links | The header's "Old screen" and the Schedule and Settings tabs' "Open the old screen (for Users, Audit, Network and the rest)" cards. R1 removes them; their words are pinned by `test_ui2_page_makes_only_1_0_presses`. |
| The engine's own 1.0 words | `fire_ice.py` `LOOK_IN_SHOW` (the cues fault for a look left in Programming) says "On 2.0, Open in 1.0 first.": 2.0 has had the Show file card since P1, so this sentence is wrong today. It is engine text (no engine change in the port PRs): change it in R1 or a small engine PR, with its tests. |
| Jeff's yes | R1 deletes 1.0 only after Jeff says so. |

Then R1 (delete 1.0) and R2 (the docs say 2.0 only). Practice mode is not part of 1.0 and not needed for R1.

## Proposed order

0, P1, P2, P3, P4, P8, P10, P11, P13, P9, P12, P5, P6, P7, P14, P15, P16, R1, R2.

Show night needs first (Programming, schedule, fireball, purge, map), then admin and setup, then removal.

## Not in this plan

| Item | Why |
|---|---|
| Resume show N sheet (play-from step 3) | DONE: 2.0 Resume and Start now offer the saved cues (start-at-cue). 2.0 only, 1.0 has no button for it. |
| Practice mode | In neither page. 2.0 has only a note that a later PR adds it. Needs Jeff's definition. |
| Redirects from old addresses | Jeff: not needed. |
| Pin rule | Jeff: dropped. |

## Rules for every port PR

| Rule | Detail |
|---|---|
| Same presses | Same route, same body, same hold and confirm as 1.0. Checked by the request-parity tables. |
| Shared rules | A pure rule 2.0 needs goes in `remote_rules.js`, never copied. Any edit to that file gets two cold reviews. |
| iPad | iPadOS 15 Safari scan stays green (`test_ui2_runs_on_ipados15`). Targets 56 px and up. |
| Tests | A selftest that fails without the change, a mutation with a `mutate_catchers.json` entry, anchors matching once. |
| Text | No em or en dashes in anything the operator reads. |
