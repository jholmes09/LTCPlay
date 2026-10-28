Booth notes

The short alert notes LTC Player plays on the private booth speaker,
never the PA. Each one is a WAV named booth_<name>.wav, at most 8
seconds long: 16, 24 or 32 bit PCM, or 32 bit float, mono or stereo.

The installer copies every booth_*.wav from packaging/windows/booth in
the repo into this folder, "Booth notes" in the LTC Player program
folder. Every install and update empties this folder first and copies
the shipped notes again, so do not change the notes here.

To use your own recording of a note instead, put a WAV with the same
name in the "Booth notes" folder inside "Settings and logs" (Start menu,
LTC Player, Settings and logs; make the folder if it is not there). That
copy always wins over the one here, and no install, update, rollback or
uninstall ever changes it. Delete it to go back to the shipped note.

The rack screen's Announcements block says how many notes were found,
like "Booth speaker: StudioLive 16R, 23 of 23 notes found", or names
the missing ones. The names are listed in OPERATOR.md and INSTALL.md.
