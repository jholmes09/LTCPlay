# Paste this into the Windows session on the Pico (the "windows-app" session)

Copy everything between the lines and paste it as one message. It installs
the newest bench build, starts the wire witness, registers and starts the
48 hour soak, tries the laser listener, and reports back. It must refuse
to go on unless Jeff has confirmed, in this chat, that the flame hardware
is disconnected.

This PC has MadMapper (licensed) but NO BEYOND for now. LTC Player still
sends its laser commands (BEYOND's OSC brightness blank and restore, the
Morning page's laser test messages) and they go to nothing. The kit's
LASER LISTENER (witness\laser_listener.ps1) stands where BEYOND would and
records every one of them; the checker judges them against the engine's
journal. The standing fault "Lasers: blanking not set up" (no "beyond"
block in ltcplay_fire_ice.json) is EXPECTED while BEYOND is absent and is
NOT a soak failure; any other fault line still is.

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

STEP 1: Get the kit. Clone or pull the repository branch bench-build
somewhere outside Program Files, for example C:\ltcplay-kit. The kit is
the folder tools\stress; it must contain witness\laser_listener.ps1 (pull
again if it does not). Print the commit you have.

STEP 2: Install the newest bench pre-release. Open
https://github.com/jholmes09/ltcplay/releases and take the newest
pre-release whose tag starts with bench- (every bench build since
2026-10-19 carries the Morning and Shutdown page). Download the
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
must be attached, or every soak show fails to start). Print whether
ltcplay_fire_ice.json has a "beyond" block (its host and port) and its
beyond_blank value (the default is "osc"). This PC has NO BEYOND: with no
beyond block the rack screen shows "Lasers: blanking not set up" and the
journal writes that line at startup and at every show start. That ONE
fault is EXPECTED while BEYOND is absent and is not a failure of anything;
every OTHER fault line is, and you paste it. Change nothing.

STEP 4: Prove the witness and the laser listener. In the kit folder run
  powershell -NoProfile -ExecutionPolicy Bypass -File .\witness\witness.ps1 -SelfTest
  powershell -NoProfile -ExecutionPolicy Bypass -File .\witness\laser_listener.ps1 -SelfTest
Each must print "self-test passed". Paste both lines.

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

STEP 5b: Try the laser listener once, in the foreground, from the kit
folder:
  powershell -NoProfile -ExecutionPolicy Bypass -File .\witness\laser_listener.ps1
EXPECTED during the soak: one line beginning "REFUSED: 127.0.0.1:8100 is
already in use by ltcplay-soak.exe" and exit code 3. The soak holds
BEYOND's OSC port itself in stage 1 and judges the laser commands in its
block report; the listener stands aside and writes nothing. Paste the
line. If it names BEYOND.exe instead, real BEYOND is running on this PC:
say so, leave it alone, change nothing. If it starts listening instead
(it prints "listen: OSC on 127.0.0.1:8100" and keeps running), leave
that window open, minimized, and say so: it writes laser.log and
laser.csv into the witness run folder.

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
  Laser listener: REFUSED by ltcplay-soak.exe (expected) / listening on
        127.0.0.1:8100 / REFUSED by BEYOND.exe (real BEYOND is here)
  Lasers: beyond block <present host:port / none, so "Lasers: blanking
        not set up" stands, EXPECTED while BEYOND is absent>
  First look: flame-violations.txt empty yes/no; status.json last_error;
        fault lines in the night journal OTHER than "Lasers: blanking not
        set up": <none / paste them>
  Anything odd: <or "nothing">
Then leave everything running. Do not stop the soak, do not restart the
PC, do not install anything else for 48 hours. If anyone needs the PC,
the stop is: "Start 48h soak.ps1" -Stop (it finishes the block first).

AFTER 48 HOURS (a later message will ask): run -Status, then double-click
tools\stress\witness\"Collect witness evidence.cmd" and report the zip's
path on the Desktop; if this PC has Python, also run
  python .\soak\soak_summary.py
and paste its verdict and the six criteria lines. A journal fault count
that is only "Lasers: blanking not set up" lines is not a failure; any
other fault line is, and you paste it.

STAGES 2 TO 4 (a later message will ask, after the soak has stopped):
start the witness AND the laser listener together, each in its own
window, from the kit folder:
  witness\"Start witness.cmd"
  witness\"Start laser listener.cmd"
The listener must print "listen: OSC on 127.0.0.1:8100" (the soak is gone,
so the port is free; if it REFUSES, paste why and stop). Both write into
the same run folder under %LOCALAPPDATA%\ltcplay\witness; "Stop witness.cmd"
stops both. After the stage, witness\"Check witness.cmd" (where Python
is) judges the flame wire and the lasers in one check_report.txt: the
laser rules are LASER BLANK MISSING, LASER BLANK NOT HEARD, LASER SILENCE,
LASER LIT OUTSIDE SHOW, LASER LIT WHILE ABORT LATCHED, LASER TEST WITHOUT
RECORD, LASER TEST WHILE SHOW LIVE, LASER TEST ON LOCKED LASER, LASER
TEST NOT HEARD, LASER TEST OFF MISSING, UNKNOWN LASER MESSAGE, FORBIDDEN
LASER ADDRESS. With no beyond block the report says the blank rules are
UNVERIFIED and EXPECTED while BEYOND is absent; that is a PASS. Paste the
verdict line and every laser line.

----------------------------------------------------------------------
