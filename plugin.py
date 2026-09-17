"""
plugin.py
Main entry point for Building Road Overlap Validator QGIS plugin.
"""
import os
from qgis.PyQt.QtGui import QIcon, QPixmap, QPainter, QColor, QPen, QBrush, QPolygonF
from qgis.PyQt.QtCore import Qt, QPointF, QRectF
from qgis.PyQt.QtWidgets import QAction
from .dialog import BuildingRoadOverlapValidatorDialog


class BuildingRoadOverlapValidatorPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dialog = None

    def _get_icon(self) -> QIcon:
        icon_path = os.path.join(os.path.dirname(__file__), "icon.png")

        # Draw a crisp QGIS-style House/Building icon (64x64)
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)

        # ── 1. Chimney ──
        painter.setBrush(QBrush(QColor(185, 28, 28)))
        painter.setPen(QPen(QColor(127, 29, 29), 1.5))
        painter.drawRect(QRectF(42, 10, 8, 14))

        # ── 2. Triangular Roof ──
        roof = QPolygonF([
            QPointF(32, 6),
            QPointF(4, 28),
            QPointF(60, 28)
        ])
        painter.setBrush(QBrush(QColor(239, 68, 68)))
        painter.setPen(QPen(QColor(153, 27, 27), 2))
        painter.drawPolygon(roof)

        # ── 3. House / Building Body ──
        painter.setBrush(QBrush(QColor(59, 130, 246)))
        painter.setPen(QPen(QColor(29, 78, 216), 2))
        painter.drawRect(QRectF(10, 28, 44, 26))

        # ── 4. Windows ──
        painter.setBrush(QBrush(QColor(224, 242, 254)))
        painter.setPen(QPen(QColor(30, 58, 138), 1.5))
        painter.drawRect(QRectF(16, 33, 9, 9))
        painter.drawRect(QRectF(39, 33, 9, 9))

        painter.setPen(QPen(QColor(30, 58, 138), 1))
        painter.drawLine(QPointF(20.5, 33), QPointF(20.5, 42))
        painter.drawLine(QPointF(16, 37.5), QPointF(25, 37.5))
        painter.drawLine(QPointF(43.5, 33), QPointF(43.5, 42))
        painter.drawLine(QPointF(39, 37.5), QPointF(48, 37.5))

        # ── 5. Front Door ──
        painter.setBrush(QBrush(QColor(245, 158, 11)))
        painter.setPen(QPen(QColor(180, 83, 9), 1.5))
        painter.drawRect(QRectF(26, 38, 12, 16))
        painter.setBrush(QBrush(QColor(255, 255, 255)))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(QPointF(35, 46), 1.2, 1.2)

        # ── 6. Bottom Road Baseline ──
        painter.setPen(QPen(QColor(217, 119, 6), 4, Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(QPointF(2, 58), QPointF(62, 58))
        painter.setPen(QPen(QColor(255, 255, 255), 1.5, Qt.DashLine))
        painter.drawLine(QPointF(4, 58), QPointF(60, 58))

        painter.end()

        try:
            pixmap.save(icon_path, "PNG")
        except Exception:
            pass

        return QIcon(pixmap)

    def initGui(self):
        icon = self._get_icon()

        self.action = QAction(icon, "Building Road Overlap Validator", self.iface.mainWindow())
        self.action.triggered.connect(self.run)

        # Add to Vector menu and Vector toolbar
        self.iface.addPluginToVectorMenu("Building Road Overlap Validator", self.action)
        self.iface.addVectorToolBarIcon(self.action)

    def unload(self):
        self.iface.removeVectorToolBarIcon(self.action)
        self.iface.removePluginVectorMenu("Building Road Overlap Validator", self.action)
        if self.dialog:
            self.dialog.close()

    def run(self):
        if not self.dialog:
            self.dialog = BuildingRoadOverlapValidatorDialog(self.iface, self.iface.mainWindow())
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()