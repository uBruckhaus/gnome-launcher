import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

BarWidget {
  id: root

  moduleName: "ubruckhaus.simple-launcher-for-fools"

  readonly property string togglePath: Qt.resolvedUrl("launcher-toggle").toString().replace(/^file:\/\//, "")

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  Process {
    id: toggleProcess
    command: []
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "\uf00a"
    tooltipText: "Simple Launcher for Fools"

    onPressed: function(mouseButton) {
      if (!root.bar) return
      toggleProcess.command = [root.togglePath]
      toggleProcess.running = true
    }
  }
}
