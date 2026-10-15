import QtQuick
import Quickshell
import qs.Commons
import qs.Ui

BarWidget {
  id: root

  moduleName: "ubruckhaus.simple-launcher-for-fools"

  readonly property string togglePath: Qt.resolvedUrl("launcher-toggle").toString().replace(/^file:\/\//, "")
  readonly property string dockPath: Qt.resolvedUrl("dock.py").toString().replace(/^file:\/\//, "")

  // The optional dock (App Manager → Settings → Dock): it quits at once when switched off,
  // and a second start only makes a running dock re-read its settings.
  Component.onCompleted: Quickshell.execDetached(["/usr/bin/python3", root.dockPath])

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "\uf00a"
    tooltipText: "App Manager"

    onPressed: function(mouseButton) {
      if (!root.bar) return
      // Detached so a click is never dropped while a previous toggle is still running.
      Quickshell.execDetached([root.togglePath])
    }
  }
}
