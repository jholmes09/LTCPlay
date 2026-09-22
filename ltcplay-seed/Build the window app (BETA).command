#!/bin/bash
# BETA. Builds "LTC Player Beta.app": the same show page in a real Mac window
# instead of a browser tab.
#
# WHAT IT IS
# A native window, a menu bar, Cmd-Q, a Dock icon, full screen. Inside the
# window is the same page, drawn by WebKit rather than by Safari. The engine
# is not changed and is not inside it.
#
# WHY IT IS BUILT THIS WAY
# The output loop has a 25 millisecond deadline, forty frames a second. If the
# screen and the engine shared a process, a window redraw or a stuck dialog
# would freeze the rig on its last frame, and from the booth the screen would
# still look alive. So this app is a WINDOW ONTO the engine, exactly as the
# browser is. Closing it does not stop a running show. That is deliberate and
# it is not going to change.
#
# WHILE IT IS BETA
#   - It is a SECOND app. 'LTC Player.app' is untouched and is what runs the
#     show until Jeff says otherwise.
#   - It is not built by 'Install ltcplay.command' and not armed by Autostart.
#   - If it will not build or will not launch, nothing is installed and the
#     proven app is still there.
set -u
cd "$(dirname "$0")" || exit 1
# This script works whether it sits in the install folder or in the
# Tools folder inside it. Asking where it IS, not guessing from what
# is beside it: a folder can be a real install and still not have
# every file this happened to look for.
[ "$(basename "$PWD")" = "Tools" ] && { cd .. || exit 1; }
for f in *.command; do [ -e "$f" ] && [ ! -x "$f" ] && chmod +x "$f" 2>/dev/null; done

APP="LTC Player Beta.app"
EXE="LTC Player Beta"
BUNDLE_ID="com.jeffholmespresents.ltcplayer.beta"
HERE="$(pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ltcbeta.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

bye() { echo; echo "$1"; echo; read -r -p "Press return to close. "; exit "${2:-0}"; }
step() { echo "  $1"; }

echo
echo "LTC Player: build the window app  (BETA)"
echo

[ -x ./.venv/bin/python ] || bye "This folder is not installed yet.
Double-click 'Install ltcplay.command' first." 1
[ -f ltcplay/cli.py ] || bye "The ltcplay program is not beside this script." 1
command -v cc >/dev/null 2>&1 || bye "The compiler is missing. In Terminal, once:
    xcode-select --install" 1
command -v codesign >/dev/null 2>&1 || bye "codesign is missing. In Terminal, once:
    xcode-select --install" 1

B="$WORK/$APP"
mkdir -p "$B/Contents/MacOS" "$B/Contents/Resources" || bye "Could not build." 1

cat > "$WORK/window.m" <<'OBJC'
/* LTC Player Beta: a native window around the show page.
 *
 * It starts the engine beside it if nothing is serving yet, waits for the
 * port to answer, and shows the page in a WKWebView. It NEVER stops the
 * engine: the engine is a separate process driving a lighting rig, and a
 * window closing is not a reason to black out a show.
 */
#import <Cocoa/Cocoa.h>
#import <WebKit/WebKit.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <mach-o/dyld.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <unistd.h>

static NSString *const kURL = @"http://127.0.0.1:7878/";
static const int kPort = 7878;

static void up(char *p) { char *s = strrchr(p, '/'); if (s) *s = '\0'; }

static void alertAndQuit(NSString *title, NSString *body) {
    NSAlert *a = [[NSAlert alloc] init];
    a.messageText = title;
    a.informativeText = body;
    a.alertStyle = NSAlertStyleCritical;
    [a addButtonWithTitle:@"OK"];
    [a runModal];
    [NSApp terminate:nil];
}

/* Is anything answering on the engine's port? */
static BOOL portIsUp(void) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) return NO;
    struct timeval tv = {0, 200000};
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    struct sockaddr_in a;
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_port = htons(kPort);
    a.sin_addr.s_addr = inet_addr("127.0.0.1");
    BOOL ok = (connect(fd, (struct sockaddr *)&a, sizeof(a)) == 0);
    close(fd);
    return ok;
}

@interface Delegate : NSObject <NSApplicationDelegate, WKNavigationDelegate>
@property (strong) NSWindow *window;
@property (strong) WKWebView *web;
@property (strong) NSString *folder;
@property (assign) BOOL startedEngine;
@end

@implementation Delegate

- (NSString *)findFolder {
    char raw[PATH_MAX], exe[PATH_MAX];
    uint32_t sz = (uint32_t)sizeof(raw);
    if (_NSGetExecutablePath(raw, &sz) != 0) return nil;
    if (!realpath(raw, exe)) return nil;
    char f[PATH_MAX];
    snprintf(f, sizeof(f), "%s", exe);
    up(f); up(f); up(f); up(f);   /* MacOS, Contents, .app, the folder */
    return [NSString stringWithUTF8String:f];
}

- (BOOL)startEngine {
    NSString *py = [self.folder stringByAppendingPathComponent:@".venv/bin/python"];
    if (![[NSFileManager defaultManager] isExecutableFileAtPath:py]) return NO;
    NSTask *t = [[NSTask alloc] init];
    t.executableURL = [NSURL fileURLWithPath:py];
    t.arguments = @[@"-m", @"ltcplay.cli", @"serve",
                    @"--port", @"7878", @"--bind", @"127.0.0.1",
                    @"--no-browser"];
    t.currentDirectoryURL = [NSURL fileURLWithPath:self.folder];
    /* An app has no terminal, so without this everything the engine says,
     * including why it could not start, goes nowhere. Same log the other
     * app writes. */
    NSString *log = [NSHomeDirectory() stringByAppendingPathComponent:
                     @"Library/Logs/LTCPlayer-start.log"];
    [[NSFileManager defaultManager] createFileAtPath:log
                                            contents:nil attributes:nil];
    NSFileHandle *fh = [NSFileHandle fileHandleForWritingAtPath:log];
    if (fh) {
        [fh seekToEndOfFile];
        t.standardOutput = fh;
        t.standardError = fh;
    }
    NSError *err = nil;
    if (![t launchAndReturnError:&err]) return NO;
    /* Deliberately not retained and never terminated: the engine outlives
     * this window, which is the whole point of them being separate. */
    return YES;
}

- (void)buildMenu {
    NSMenu *bar = [[NSMenu alloc] init];
    NSMenuItem *appItem = [[NSMenuItem alloc] init];
    [bar addItem:appItem];
    NSMenu *appMenu = [[NSMenu alloc] init];
    [appMenu addItemWithTitle:@"About LTC Player Beta"
                       action:@selector(orderFrontStandardAboutPanel:)
                keyEquivalent:@""];
    [appMenu addItem:[NSMenuItem separatorItem]];
    [appMenu addItemWithTitle:@"Hide" action:@selector(hide:) keyEquivalent:@"h"];
    [appMenu addItem:[NSMenuItem separatorItem]];
    [appMenu addItemWithTitle:@"Quit LTC Player Beta"
                       action:@selector(terminate:) keyEquivalent:@"q"];
    appItem.submenu = appMenu;

    NSMenuItem *editItem = [[NSMenuItem alloc] init];
    [bar addItem:editItem];
    NSMenu *edit = [[NSMenu alloc] initWithTitle:@"Edit"];
    [edit addItemWithTitle:@"Cut" action:@selector(cut:) keyEquivalent:@"x"];
    [edit addItemWithTitle:@"Copy" action:@selector(copy:) keyEquivalent:@"c"];
    [edit addItemWithTitle:@"Paste" action:@selector(paste:) keyEquivalent:@"v"];
    [edit addItemWithTitle:@"Select All" action:@selector(selectAll:)
             keyEquivalent:@"a"];
    editItem.submenu = edit;

    NSMenuItem *viewItem = [[NSMenuItem alloc] init];
    [bar addItem:viewItem];
    NSMenu *view = [[NSMenu alloc] initWithTitle:@"View"];
    [view addItemWithTitle:@"Reload the page" action:@selector(reloadPage:)
             keyEquivalent:@"r"];
    [[view addItemWithTitle:@"Enter Full Screen"
                     action:@selector(toggleFullScreen:) keyEquivalent:@"f"]
        setKeyEquivalentModifierMask:NSEventModifierFlagControl
                                     | NSEventModifierFlagCommand];
    viewItem.submenu = view;
    NSApp.mainMenu = bar;
}

- (void)reloadPage:(id)sender { [self.web reload]; }

- (void)applicationDidFinishLaunching:(NSNotification *)n {
    self.folder = [self findFolder];
    if (!self.folder) {
        alertAndQuit(@"LTC Player Beta cannot start.",
                     @"It could not work out which folder it is in.");
        return;
    }
    if (!portIsUp()) {
        if (![self startEngine]) {
            alertAndQuit(@"LTC Player Beta cannot start.",
                         [NSString stringWithFormat:
                          @"The Python environment is missing or would not "
                          @"start.\n\nOpen this folder and double-click "
                          @"'Install ltcplay.command':\n\n%@", self.folder]);
            return;
        }
        self.startedEngine = YES;
    }

    NSRect frame = NSMakeRect(0, 0, 1180, 820);
    self.window = [[NSWindow alloc]
        initWithContentRect:frame
                  styleMask:(NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                             | NSWindowStyleMaskMiniaturizable
                             | NSWindowStyleMaskResizable)
                    backing:NSBackingStoreBuffered
                      defer:NO];
    self.window.title = @"LTC Player  (beta)";
    self.window.minSize = NSMakeSize(520, 520);
    [self.window setFrameAutosaveName:@"LTCPlayerBetaWindow"];

    WKWebViewConfiguration *cfg = [[WKWebViewConfiguration alloc] init];
    self.web = [[WKWebView alloc] initWithFrame:frame configuration:cfg];
    self.web.navigationDelegate = self;
    self.web.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
    self.window.contentView = self.web;
    [self.window center];
    [self.window makeKeyAndOrderFront:nil];
    [NSApp activateIgnoringOtherApps:YES];

    [self loadWhenReady:0];
}

/* Wait for the engine before loading, so the window never opens on a
 * connection error and sit there looking broken. */
- (void)loadWhenReady:(int)tries {
    if (portIsUp()) {
        [self.web loadRequest:[NSURLRequest requestWithURL:
                               [NSURL URLWithString:kURL]]];
        return;
    }
    if (tries > 150) {
        alertAndQuit(@"The show page never came up.",
                     @"The engine did not start listening on port 7878. "
                     @"Something else may be using it. There is a record in "
                     @"~/Library/Logs/LTCPlayer-start.log");
        return;
    }
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.1 * NSEC_PER_SEC)),
                   dispatch_get_main_queue(), ^{
        [self loadWhenReady:tries + 1];
    });
}

- (void)webView:(WKWebView *)w didFailProvisionalNavigation:(WKNavigation *)nav
      withError:(NSError *)error {
    alertAndQuit(@"The show page would not load.",
                 [NSString stringWithFormat:@"%@\n\n%@", kURL,
                  error.localizedDescription]);
}

/* Closing the window quits this app. It does NOT stop the engine: the rig
 * keeps running, and the browser or another window can come back to it. */
- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)a {
    return YES;
}
@end

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        [NSApplication sharedApplication];
        [NSApp setActivationPolicy:NSApplicationActivationPolicyRegular];
        Delegate *d = [[Delegate alloc] init];
        NSApp.delegate = d;
        [d buildMenu];
        [NSApp run];
    }
    return 0;
}
OBJC

if ! cc -fobjc-arc -O2 -Wall -o "$B/Contents/MacOS/$EXE" "$WORK/window.m" \
      -framework Cocoa -framework WebKit 2>"$WORK/cc.err"; then
  bye "The window app would not compile:

$(head -12 "$WORK/cc.err")

Nothing was built. 'LTC Player.app' is untouched." 1
fi
chmod +x "$B/Contents/MacOS/$EXE"
step "window app compiled"

cat > "$B/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>LTC Player Beta</string>
  <key>CFBundleDisplayName</key><string>LTC Player Beta</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleExecutable</key><string>$EXE</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>beta</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>LTC Player listens to an audio input to read SMPTE timecode and run the lighting show.</string>
  <key>NSDesktopFolderUsageDescription</key><string>LTC Player reads the sequences and the show file in the folder it was installed into.</string>
  <key>NSDocumentsFolderUsageDescription</key><string>LTC Player reads the sequences and the show file in the folder it was installed into.</string>
  <key>NSDownloadsFolderUsageDescription</key><string>LTC Player reads the sequences and the show file in the folder it was installed into.</string>
  <key>NSRemovableVolumesUsageDescription</key><string>LTC Player reads the sequences and the show file in the folder it was installed into.</string>
  <key>NSAppTransportSecurity</key>
  <dict>
    <key>NSAllowsLocalNetworking</key><true/>
    <key>NSExceptionDomains</key>
    <dict>
      <key>127.0.0.1</key>
      <dict>
        <key>NSExceptionAllowsInsecureHTTPLoads</key><true/>
        <key>NSIncludesSubdomains</key><true/>
      </dict>
    </dict>
  </dict>
</dict>
</plist>
PLIST
# NSAppTransportSecurity: without it WebKit refuses a plain http:// page and
# the window opens completely blank, with no error anywhere.
step "Info.plist written"

if [ -d "$HERE/LTC Player.iconset" ] && command -v iconutil >/dev/null 2>&1; then
  iconutil -c icns "$HERE/LTC Player.iconset" \
    -o "$B/Contents/Resources/icon.icns" 2>/dev/null \
    && step "icon built" || step "no icon; carrying on"
fi

if ! codesign --force --sign - --identifier "$BUNDLE_ID" --timestamp=none \
      "$B" >"$WORK/sign.err" 2>&1; then
  bye "codesign refused to sign it:

$(head -5 "$WORK/sign.err")

Nothing was built." 1
fi
if ! codesign --verify --deep --strict "$B" >"$WORK/ver.err" 2>&1; then
  bye "It does not verify after signing:

$(head -5 "$WORK/ver.err")

Nothing was built." 1
fi
step "signed and verified"

rm -rf "$HERE/$APP" 2>/dev/null
/usr/bin/ditto "$B" "$HERE/$APP" || bye "Built, but could not be copied here." 1
codesign --verify --deep --strict "$HERE/$APP" >/dev/null 2>&1 || {
  rm -rf "$HERE/$APP"
  bye "It lost its signature on the way into this folder. Nothing installed." 1
}
step "installed as $APP"

cat <<EOF

Built: $HERE/$APP

  This is BETA and it is a SECOND app. 'LTC Player.app' is untouched and is
  still what runs the show.

WHAT TO EXPECT

  Double-click it. A real window opens with the show page in it, a menu bar,
  Cmd-Q, and Ctrl-Cmd-F for full screen. Cmd-R reloads the page.

  If the engine is already running it attaches to it. If not it starts one.

  CLOSING THE WINDOW DOES NOT STOP THE SHOW. The engine is a separate
  process driving the rig, and a window closing is not a reason to black
  out a rig. Stop the show on the page first if that is what you meant.

WHAT TO WATCH FOR, AND TELL ME

  A blank white window, a window that never appears, the page loading but
  buttons doing nothing, or anything slower than the browser.

EOF
read -r -p "Press return to close. "
