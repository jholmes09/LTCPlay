# Paste this into the Windows session on the Pico (the "windows-app" session)

Copy everything between the lines and paste it as one message. It installs
the newest bench build, starts the wire witness, registers and starts the
48 hour soak, and reports back. It must refuse to go on unless Jeff has
confirmed, in this chat, that the flame hardware is disconnected.

----------------------------------------------------------------------

You are the Windows session on the Pico show PC. Jeff wants the 48 hour
stress soak started now. Follow this exactly, in order, and stop at the
first step that fails. Never use Task Manager's End task on flamesafe,
ltcplay or LTC Player. Nothing you do may arm or fire real hardware.

STEP 0: THE GATE. Ask Jeff this one question and wait for his answer:
"Is the flame hardware DISCONNECTED from this PC: data cable to the flame
node unplugged, and gas off? Answer with the word DISCONNECTED."
If the answer is anything but the single word DISCONNECTED, stop and say
so. Do not go on to any other step. Do not infer it, do not assume it.

STEP 1: Get the kit. Clone or pull the repository branch stress-kit (or
bench-build once the PR has merged) somewhere outside Program Files, for
example C:\ltcplay-kit. The kit is the folder tools\stress. Print the
commit you have.

STEP 2: Install the newest bench pre-release. Open
https://github.com/jholmes09/ltcplay/releases and take the newest
pre-release whose tag starts with bench- (today that is newer than
bench-2026.10.16-052db85; the Morning and Shutdown page build will appear
there when PR 68 merges, and it is fine if it has not yet). Download the
installer and its .sha256 beside it, check the hash with Get-FileHash, and
only if it matches run the installer: no show may be running (the rack
screen says what is on; the installer refuses otherwise). Leave autostart
ticked. When the rack screen is back, read its header and print the
exact release and build string. It must match the file you ran. If the
header says MODIFIED SINCE, stop and say so.

STEP 3: Settings check. Print showpc.json and confirm flamesafe_config
points at a flamesafe.json that exists, show_mode is fire_ice, and
ltcplay_fire_ice.json beside the schedule says scheduler_performs true and
auto_start when_run_pressed. Print flamesafe.json's destination (it is
127.0.0.1 unless Andy has confirmed the node; either is fine for the soak,
which forces its own copy to 127.0.0.1) and whether confirmed is true.
Print which audio output devices Windows sees (the Scarlett or PreSonus
must be attached, or every soak show fails to start). Change nothing.

STEP 4: Prove the witness. In the kit folder run
  powershell -NoProfile -ExecutionPolicy Bypass -File .\witness\witness.ps1 -SelfTest
It must print "self-test passed". Paste the line.

STEP 5: Start the soak and the companion witness. In an ELEVATED
PowerShell, from the kit folder:
  powershell -NoProfile -ExecutionPolicy Bypass -File ".\soak\Start 48h soak.ps1" -FlameHardwareDisconnected -Confirmation DISCONNECTED
Only pass -Confirmation DISCONNECTED because Jeff typed that word in STEP
0. If MadMapper and BEYOND are installed on this PC and there is a restart
wrapper for them, add -RestartCmd "<that command>"; otherwise leave it
out. The script writes unattended.json (contain true), registers the task
"LTC Player unattended soak" for 2 days, starts it, starts the witness in
companion mode, and prints status.json when the first block is up. Paste
its output. If it REFUSED, paste why and stop.

STEP 6: Verify after 10 minutes. Run
  powershell -NoProfile -ExecutionPolicy Bypass -File ".\soak\Start 48h soak.ps1" -Status
and confirm: phase "block running", block 1, and that
%LOCALAPPDATA%\ltcplay\soak\<stamp> block 1\block 1 report.txt exists and
its "Show audio" line names a real interface (not "NO interface found").
Open flame-violations.txt in that folder if it exists: it must be empty.
Check Task Scheduler shows the task Running. Check the witness window is
up (a minimized PowerShell titled witness) and %LOCALAPPDATA%\ltcplay\
witness\<stamp>\procs.csv is growing.

STEP 7: Report back in this chat, in this form:
  Build installed: <release and build string from the header>
  Gate: Jeff typed DISCONNECTED at <time>
  Witness self-test: passed / failed
  Soak: block 1 running since <time>, audio interface <name>, contain
        true, task registered for 2 days, runs until <date time>
  Witness: companion mode, folder <path>
  First look: flame-violations.txt empty yes/no; status.json last_error
  Anything odd: <or "nothing">
Then leave everything running. Do not stop the soak, do not restart the
PC, do not install anything else for 48 hours. If anyone needs the PC,
the stop is: "Start 48h soak.ps1" -Stop (it finishes the block first).

AFTER 48 HOURS (a later message will ask): run -Status, then double-click
tools\stress\witness\"Collect witness evidence.cmd" and report the zip's
path on the Desktop; if this PC has Python, also run
  python .\soak\soak_summary.py
and paste its verdict and the six criteria lines.

----------------------------------------------------------------------
