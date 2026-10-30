import qs.modules.common
import qs.modules.common.widgets

import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Io

// Progress of fake-typer (~/Code/fake-typer). Reads the status file it writes to
// $XDG_RUNTIME_DIR/fake-typer/status.json. Display only: Super+Alt+V pauses/resumes,
// double-tapping it stops.
Item {
    id: root

    property var status: ({})
    property real now: Date.now() / 1000

    readonly property string state: status.state ?? ""
    readonly property real age: now - (status.updated ?? 0)
    readonly property bool running: ["planning", "typing", "paused"].includes(state) && age < 8
    readonly property bool finished: ["done", "stopped", "error"].includes(state) && age < 4
    readonly property bool active: running || finished

    visible: active
    Layout.fillHeight: true
    implicitWidth: rowLayout.implicitWidth + rowLayout.spacing * 2
    implicitHeight: Appearance.sizes.barHeight

    function formatTime(seconds) {
        const s = Math.max(0, Math.round(seconds));
        return `${Math.floor(s / 60)}:${(s % 60).toString().padStart(2, "0")}`;
    }

    readonly property string label: {
        const pct = `${Math.round((status.progress ?? 0) * 100)}%`;
        switch (state) {
        case "planning": return status.message || "Planning…";
        case "typing": return `Typing ${pct} · ${formatTime(status.remaining ?? 0)} left`;
        case "paused": return `Paused ${pct}`;
        case "done": return "Done typing";
        case "stopped": return `Stopped at ${pct}`;
        case "error": return status.message || "Error";
        }
        return "";
    }

    readonly property string icon: ({
        planning: "auto_awesome", typing: "keyboard", paused: "pause",
        done: "check", stopped: "stop", error: "error"
    })[state] ?? "keyboard"

    FileView {
        id: statusFile
        path: Quickshell.env("XDG_RUNTIME_DIR") + "/fake-typer/status.json"
        watchChanges: true
        printErrors: false // the file only exists once fake-typer has run
        onFileChanged: reload()
        onLoaded: {
            try {
                root.status = JSON.parse(text());
            } catch (e) {}
        }
    }

    Timer {
        // Fallback for missed file events, and to age out stale/finished states.
        interval: root.running ? 500 : 1500
        running: true
        repeat: true
        onTriggered: {
            root.now = Date.now() / 1000;
            statusFile.reload();
        }
    }

    MouseArea {
        id: mouseArea
        anchors.fill: parent
        hoverEnabled: true
        property bool hovered: false
        onEntered: hovered = true
        onExited: hovered = false
        acceptedButtons: Qt.NoButton

        PopupToolTip {
            extraVisibleCondition: (root.status.preview ?? "").length > 0
            text: `${root.status.preview ?? ""}${(root.status.chars ?? 0) > 80 ? "…" : ""}\n\n`
                + `${root.status.chars ?? 0} characters · ${root.status.typos ?? 0} typos planned\n`
                + "Super+Alt+V: pause/resume · twice quickly: stop"
        }
    }

    RowLayout {
        id: rowLayout
        spacing: 4
        anchors.fill: parent

        ClippedFilledCircularProgress {
            id: progressCircle
            Layout.alignment: Qt.AlignVCenter
            lineWidth: Appearance.rounding.unsharpen
            value: root.state === "planning" ? 0 : (root.status.progress ?? 0)
            implicitSize: 20
            colPrimary: root.state === "error" ? Appearance.colors.colError : Appearance.colors.colOnSecondaryContainer
            enableAnimation: true

            Item {
                anchors.centerIn: parent
                width: progressCircle.implicitSize
                height: progressCircle.implicitSize

                MaterialSymbol {
                    anchors.centerIn: parent
                    fill: 1
                    text: root.icon
                    iconSize: Appearance.font.pixelSize.normal
                    color: Appearance.m3colors.m3onSecondaryContainer
                }
            }
        }

        StyledText {
            Layout.alignment: Qt.AlignVCenter
            Layout.fillWidth: true
            Layout.rightMargin: rowLayout.spacing
            horizontalAlignment: Text.AlignHCenter
            elide: Text.ElideRight
            color: Appearance.colors.colOnLayer1
            text: root.label
        }
    }
}
