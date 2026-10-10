import QtQuick
import Quickshell
import qs.Commons
import qs.Ui

BarWidget {
  id: root

  moduleName: "ubruckhaus.simple-launcher-for-fools"

  readonly property string togglePath: Qt.resolvedUrl("launcher-toggle").toString().replace(/^file:\/\//, "")

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "\uf00a"
    tooltipText: "Launch & Bind"

    onPressed: function(mouseButton) {
      if (!root.bar) return
      // Detached so a click is never dropped while a previous toggle is still running.
      Quickshell.execDetached([root.togglePath])
    }
  }
}
