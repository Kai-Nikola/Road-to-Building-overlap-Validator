"""
dialog.py
PyQt5 GUI for Building Road Overlap Validator.
Uses native QgsMapLayerComboBox dropdowns to select layers already loaded in QGIS.
"""

import os
import subprocess
import sys
from qgis.PyQt.QtCore import Qt, QThread, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QPushButton, QCheckBox, QProgressBar, QTextEdit, QFileDialog,
    QMessageBox, QGroupBox, QFrame, QLineEdit
)
from qgis.gui import QgsMapLayerComboBox
from qgis.core import (
    QgsProject, QgsMapLayerProxyModel, QgsVectorLayer
)

from .processor import run_correction


class WorkerThread(QThread):
    progress_changed = pyqtSignal(int, int)
    stats_updated = pyqtSignal(dict)
    log_message = pyqtSignal(str)
    finished_success = pyqtSignal(dict, str)
    finished_error = pyqtSignal(str)

    def __init__(self, params):
        super().__init__()
        self.params = params

    def run(self):
        try:
            stats = run_correction(
                road_path=self.params["road_path"],
                poly_path=self.params["poly_path"],
                output_path=self.params["output_path"],
                aoi_path=self.params["aoi_path"],
                enable_trim=self.params["enable_trim"],
                enable_delete=self.params["enable_delete"],
                enable_move=self.params["enable_move"],
                progress_callback=lambda c, t: self.progress_changed.emit(c, t),
                stats_callback=lambda s: self.stats_updated.emit(s),
                log_callback=lambda m: self.log_message.emit(m),
            )
            self.finished_success.emit(stats, self.params["output_path"])
        except Exception as exc:
            import traceback
            traceback.print_exc()
            self.finished_error.emit(str(exc))


class MetricCard(QFrame):
    def __init__(self, label_text: str, color_hex: str, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"""
            QFrame {{
                background-color: #1E2533;
                border: 1px solid {color_hex};
                border-radius: 5px;
                padding: 4px;
            }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(2)

        title = QLabel(label_text.upper())
        title.setStyleSheet("font-size: 9px; font-weight: bold; color: #8C9BAE; border: none;")
        self.val_label = QLabel("0")
        self.val_label.setStyleSheet(f"font-size: 20px; font-weight: bold; color: {color_hex}; border: none;")

        layout.addWidget(title)
        layout.addWidget(self.val_label)

    def set_value(self, val):
        self.val_label.setText(f"{val:,}")


class BuildingRoadOverlapValidatorDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.setWindowTitle("Building Road Overlap Validator")
        self.resize(940, 740)
        self._last_output_dir = ""
        self._build_ui()

    def _build_ui(self):
        main_layout = QHBoxLayout(self)

        # ── Left Column: Config & Actions ───────────────────────────
        left_pane = QVBoxLayout()

        # Input Layers (QGIS Layer Dropdowns)
        in_group = QGroupBox("Input Layers (Loaded in QGIS)")
        in_layout = QGridLayout(in_group)
        in_layout.setVerticalSpacing(10)

        in_layout.addWidget(QLabel("Road Layer (Line):"), 0, 0)
        self.road_combo = QgsMapLayerComboBox()
        self.road_combo.setFilters(QgsMapLayerProxyModel.LineLayer)
        in_layout.addWidget(self.road_combo, 0, 1)

        in_layout.addWidget(QLabel("Building Polygon Layer:"), 1, 0)
        self.poly_combo = QgsMapLayerComboBox()
        self.poly_combo.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        in_layout.addWidget(self.poly_combo, 1, 1)

        in_layout.addWidget(QLabel("AOI Layer (Optional Mask):"), 2, 0)
        self.aoi_combo = QgsMapLayerComboBox()
        self.aoi_combo.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        self.aoi_combo.setAllowEmptyLayer(True)
        in_layout.addWidget(self.aoi_combo, 2, 1)

        left_pane.addWidget(in_group)

        # Actions Box
        act_group = QGroupBox("Validation & Correction Actions")
        act_layout = QVBoxLayout(act_group)
        self.chk_trim = QCheckBox("Enable Trimming (Trim Conflict Edges)")
        self.chk_trim.setChecked(True)
        self.chk_move = QCheckBox("Enable Shifting (Smart Move Patches)")
        self.chk_move.setChecked(True)
        self.chk_delete = QCheckBox("Enable Purging (Delete Small Slivers <3m²)")
        self.chk_delete.setChecked(True)
        act_layout.addWidget(self.chk_trim)
        act_layout.addWidget(self.chk_move)
        act_layout.addWidget(self.chk_delete)
        left_pane.addWidget(act_group)

        # Output Box
        out_group = QGroupBox("Output")
        out_layout = QVBoxLayout(out_group)
        out_lbl = QLabel("Output Shapefile:")
        out_lbl.setStyleSheet("font-weight: bold; font-size: 11px;")
        out_layout.addWidget(out_lbl)

        out_row = QHBoxLayout()
        self.out_edit = QLineEdit()
        self.out_edit.setPlaceholderText("Click 'Save As…' to specify destination .shp file...")
        out_row.addWidget(self.out_edit, 1)

        self.btn_save_as = QPushButton("Save As…")
        self.btn_save_as.setStyleSheet("padding: 5px 14px; font-weight: bold;")
        self.btn_save_as.clicked.connect(self._browse_save_as)
        out_row.addWidget(self.btn_save_as)

        out_layout.addLayout(out_row)
        left_pane.addWidget(out_group)

        # Progress
        self.prog_label = QLabel("Ready")
        left_pane.addWidget(self.prog_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        left_pane.addWidget(self.progress_bar)

        # Log Window
        left_pane.addWidget(QLabel("Log Output:"))
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setStyleSheet(
            "background-color: #12151D; color: #D1D5DB; font-family: Monospace; font-size: 11px;"
        )
        left_pane.addWidget(self.log_text)

        # Run + Open Output Folder Buttons
        btn_row = QHBoxLayout()
        self.btn_run = QPushButton("▶  RUN VALIDATION & CORRECTION")
        self.btn_run.setStyleSheet("""
            QPushButton {
                background-color: #2563EB;
                color: white;
                font-weight: bold;
                padding: 10px;
                border-radius: 4px;
                font-size: 12px;
            }
            QPushButton:hover { background-color: #1D4ED8; }
            QPushButton:disabled { background-color: #4B5563; }
        """)
        self.btn_run.clicked.connect(self._run_clicked)
        btn_row.addWidget(self.btn_run, 3)

        self.btn_open_folder = QPushButton("📂 Open Output Folder")
        self.btn_open_folder.setStyleSheet("padding: 10px; font-weight: bold;")
        self.btn_open_folder.setEnabled(False)
        self.btn_open_folder.clicked.connect(self._open_output_dir)
        btn_row.addWidget(self.btn_open_folder, 1)

        left_pane.addLayout(btn_row)
        main_layout.addLayout(left_pane, 65)

        # ── Right Column: Statistics ───────────────────────────────
        right_group = QGroupBox("Live Metrics")
        right_layout = QVBoxLayout(right_group)

        self.cards = {
            "total_overlap": MetricCard("Total Overlaps", "#3B82F6"),
            "corrected":     MetricCard("Corrected (Trim)", "#10B981"),
            "moved":         MetricCard("Shifted (Patch)",  "#60A5FA"),
            "deleted":       MetricCard("Purged (<3m²)",    "#EF4444"),
            "unresolved":    MetricCard("Unresolved",       "#A78BFA"),
        }
        for card in self.cards.values():
            right_layout.addWidget(card)

        right_layout.addStretch()
        main_layout.addWidget(right_group, 35)

    def _get_layer_file_path(self, combo: QgsMapLayerComboBox) -> str:
        """Extracts the underlying file path from the selected QGIS vector layer."""
        layer = combo.currentLayer()
        if not layer or not layer.isValid():
            return None
        source = layer.source()
        if "|" in source:
            source = source.split("|")[0]
        return source

    def _browse_save_as(self):
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Save Corrected Shapefile", "", "Shapefiles (*.shp)"
        )
        if file_path:
            if not file_path.lower().endswith(".shp"):
                file_path += ".shp"
            self.out_edit.setText(file_path)

    def _open_output_dir(self):
        if self._last_output_dir and os.path.isdir(self._last_output_dir):
            if sys.platform == "win32":
                os.startfile(self._last_output_dir)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", self._last_output_dir])
            else:
                subprocess.Popen(["xdg-open", self._last_output_dir])

    def _append_log(self, text: str):
        self.log_text.append(text)
        self.log_text.verticalScrollBar().setValue(self.log_text.verticalScrollBar().maximum())

    def _update_stats(self, stats: dict):
        for k, v in stats.items():
            if k in self.cards:
                self.cards[k].set_value(v)

    def _update_progress(self, curr: int, total: int):
        pct = int(curr / total * 100) if total else 0
        self.progress_bar.setValue(pct)
        self.prog_label.setText(f"Processing patch {curr:,} / {total:,} ({pct}%)")

    def _run_clicked(self):
        road_src = self._get_layer_file_path(self.road_combo)
        poly_src = self._get_layer_file_path(self.poly_combo)
        aoi_src  = self._get_layer_file_path(self.aoi_combo)
        out_src  = self.out_edit.text().strip()

        if not road_src or not os.path.isfile(road_src):
            QMessageBox.critical(self, "Input Error", "Please select a valid Road line layer currently open in QGIS.")
            return
        if not poly_src or not os.path.isfile(poly_src):
            QMessageBox.critical(self, "Input Error", "Please select a valid Building polygon layer currently open in QGIS.")
            return
        if not out_src:
            QMessageBox.critical(self, "Input Error", "Please specify an output path via 'Save As…'.")
            return

        if not aoi_src:
            res = QMessageBox.question(
                self, "AOI Confirmation",
                "No AOI layer selected. Do you want to process all polygons without an AOI mask?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes
            )
            if res != QMessageBox.Yes:
                return

        # Start process
        self.btn_run.setEnabled(False)
        self.btn_open_folder.setEnabled(False)
        self.progress_bar.setValue(0)
        self.log_text.clear()
        for card in self.cards.values():
            card.set_value(0)

        params = {
            "road_path": road_src,
            "poly_path": poly_src,
            "aoi_path": aoi_src if (aoi_src and os.path.isfile(aoi_src)) else None,
            "output_path": out_src,
            "enable_trim": self.chk_trim.isChecked(),
            "enable_delete": self.chk_delete.isChecked(),
            "enable_move": self.chk_move.isChecked()
        }

        self.worker = WorkerThread(params)
        self.worker.progress_changed.connect(self._update_progress)
        self.worker.stats_updated.connect(self._update_stats)
        self.worker.log_message.connect(self._append_log)
        self.worker.finished_success.connect(self._on_success)
        self.worker.finished_error.connect(self._on_error)
        self.worker.start()

    def _on_success(self, stats: dict, output_path: str):
        self.btn_run.setEnabled(True)
        self._last_output_dir = os.path.dirname(output_path)
        self.btn_open_folder.setEnabled(True)
        self.progress_bar.setValue(100)
        self.prog_label.setText("Completed Successfully ✓")
        self._update_stats(stats)

        # Automatically load results into QGIS layer panel
        base_dir = os.path.dirname(output_path)
        base_name = os.path.splitext(os.path.basename(output_path))[0]
        if base_name.endswith("_polygons"):
            base_name = base_name[:-9]

        files_to_load = [
            (output_path, f"{base_name} (Corrected Polygons)"),
            (os.path.join(base_dir, f"{base_name}_edited_pts.shp"), f"{base_name} (Edited Pts)"),
            (os.path.join(base_dir, f"{base_name}_moved_pts.shp"), f"{base_name} (Moved Pts)"),
            (os.path.join(base_dir, f"{base_name}_deleted_pts.shp"), f"{base_name} (Deleted Pts)"),
            (os.path.join(base_dir, f"{base_name}_trimmed_parts.shp"), f"{base_name} (Trimmed Parts)"),
        ]

        for file_path, layer_name in files_to_load:
            if os.path.isfile(file_path):
                vlayer = QgsVectorLayer(file_path, layer_name, "ogr")
                if vlayer.isValid():
                    QgsProject.instance().addMapLayer(vlayer)

        QMessageBox.information(
            self, "Complete",
            f"Validation & Correction complete!\nLayers loaded into QGIS canvas.\nOutput: {output_path}"
        )

    def _on_error(self, err_msg: str):
        self.btn_run.setEnabled(True)
        self.prog_label.setText("Processing Failed ✗")
        self._append_log(f"\n[ERROR] {err_msg}")
        QMessageBox.critical(self, "Processing Error", f"Processing encountered an error:\n{err_msg}")