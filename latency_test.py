import sys
import random
import re
import shutil
import os
import jack
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QPushButton, QLabel, QTabWidget,
                             QTextEdit, QLineEdit, QComboBox, QCheckBox)
from PyQt6.QtCore import Qt, QTimer, QProcess, pyqtSignal, QPoint
from PyQt6.QtGui import (QColor, QPainter, QBrush, QPalette, QPen,
                         QFont, QPixmap, QGuiApplication, QTextCursor) # Keep necessary imports

# Add custom handler for unraisable exceptions (Good practice)
def custom_unraisable_hook(unraisable):
    """
    Custom handler for unraisable exceptions that filters out JACK callback errors
    and other known harmless exceptions from JACK-related operations.
    """
    # Convert error message and traceback to string for easier pattern matching
    err_msg = str(unraisable.err_msg) if hasattr(unraisable, 'err_msg') else ''
    exc_value = str(unraisable.exc_value) if hasattr(unraisable, 'exc_value') else ''
    
    # Patterns that indicate JACK-related callback errors we want to suppress
    suppress_patterns = [
        ('cffi callback', 'callback_wrapper'),  # JACK port registration callbacks
        ('jack.py', 'AssertionError'),          # Port wrapping assertions
        ('_wrap_port_ptr', 'assert False'),     # Specific port wrapping failures
    ]
    
    # Check if any of our patterns match the error
    for pattern in suppress_patterns:
        if all(p.lower() in err_msg.lower() or p.lower() in exc_value.lower() for p in pattern):
            return  # Silently ignore these errors
            
    # For other unraisable exceptions, use the default handler
    sys.__unraisablehook__(unraisable)

# Install the custom handler early
sys.unraisablehook = custom_unraisable_hook


class LatencyTesterApp(QMainWindow):
    # PyQt signals for port registration events
    port_registered = pyqtSignal(str, bool)  # port name, is_input
    port_unregistered = pyqtSignal(str, bool)  # port name, is_input (Though unused in latency test directly, keep for consistency if callbacks are used)

    def __init__(self):
        super().__init__()
        self.setWindowTitle('jack_delay GUI')
        self.setGeometry(150, 150, 800, 600) # Adjusted size

        # --- Dependencies copied/adapted from JackConnectionManager ---
        self.client = jack.Client('LatencyTester')
        self.dark_mode = self.is_dark_mode()
        self.setup_colors()
        self.callbacks_enabled = True # Assume enabled for latency test functionality

        # Latency test variables
        self.latency_process = None
        self.latency_values = []
        self.latency_timer = QTimer()
        self.latency_waiting_for_connection = False # Flag to wait for connection
        # Store selected physical port aliases for latency test
        self.latency_selected_input_alias = None
        self.latency_selected_output_alias = None
        # --- End Dependencies ---

        # Set up JACK port registration callbacks
        # Need this for auto-connection triggering
        self.client.set_port_registration_callback(self._handle_port_registration)
        self.port_registered.connect(self._on_port_registered)
        # self.port_unregistered.connect(self._on_port_unregistered) # Not strictly needed for latency test

        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)

        # Setup the latency tab directly in the main layout
        self.setup_latency_tab(main_layout) # Pass the main layout

        # Activate JACK client
        try:
            self.client.activate()
        except jack.JackError as e:
            print(f"Failed to activate JACK client: {e}")
            # Optionally show an error message to the user
            error_label = QLabel(f"Error: Could not connect to JACK server.\n{e}")
            error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            main_layout.addWidget(error_label)
            # Disable relevant buttons if JACK fails
            if hasattr(self, 'latency_run_button'):
                self.latency_run_button.setEnabled(False)
            if hasattr(self, 'latency_refresh_button'):
                self.latency_refresh_button.setEnabled(False)


    # --- Latency Test Methods (Copied from JackConnectionManager) ---

    def run_latency_test(self):
        """Starts the jack_delay process and timer."""
        if self.latency_process is not None and self.latency_process.state() != QProcess.ProcessState.NotRunning:
            self.latency_results_text.append("Test already in progress.")
            return

        # Refresh combo boxes with latest ports
        self._populate_latency_combos()

        self.latency_run_button.setEnabled(False)
        self.latency_stop_button.setEnabled(True) # Enable Stop button
        self.latency_results_text.clear() # Clear previous results/messages

        if self.latency_raw_output_checkbox.isChecked():
             self.latency_results_text.setText("Starting latency test (Raw Output)...\n"
                                               "Select ports if not already selected.\n"
                                               "Attempting auto-connection...\n")
        else:
             self.latency_results_text.setText("Starting latency test (Average)...\n"
                                               "Select ports if not already selected.\n"
                                               "Attempting auto-connection...\n"
                                               "Waiting for measurement signal...\n") # Updated message

        self.latency_values = []
        # Only wait for connection signal if NOT showing raw output
        self.latency_waiting_for_connection = not self.latency_raw_output_checkbox.isChecked()

        self.latency_process = QProcess()
        self.latency_process.readyReadStandardOutput.connect(self.handle_latency_output)
        self.latency_process.finished.connect(self.handle_latency_finished)
        self.latency_process.errorOccurred.connect(self.handle_latency_error)

        # Determine command
        # Try jack_delay first, then jack_iodelay as fallback
        program = shutil.which("jack_delay")
        if program is None:
            program = shutil.which("jack_iodelay")

        # If neither is found, show error and exit
        if program is None:
             self.latency_results_text.setText("Error: Neither 'jack_delay' nor 'jack_iodelay' found.\n"
                                               "Depending on your distribution, install jack-delay, jack_delay or jack-example-tools (jack_iodelay).")
             self.latency_run_button.setEnabled(True)  # Re-enable run button
             self.latency_stop_button.setEnabled(False) # Ensure stop is disabled
             self.latency_process = None # Clear the process object
             return # Stop execution

        arguments = []

        self.latency_process.setProgram(program) # Use the found program path
        self.latency_process.setArguments(arguments)
        self.latency_process.start() # Start the process
        # Connection attempt is now triggered by _on_port_registered when jack_delay ports appear.

    def handle_latency_output(self):
        """Handles output from the jack_delay process."""
        if self.latency_process is None:
            return

        data = self.latency_process.readAllStandardOutput().data().decode()

        if self.latency_raw_output_checkbox.isChecked():
            # Raw output mode: Append data directly
            self.latency_results_text.moveCursor(QTextCursor.MoveOperation.End)
            self.latency_results_text.insertPlainText(data)
            self.latency_results_text.moveCursor(QTextCursor.MoveOperation.End)
        else:
            # Average calculation mode (original logic)
            # Check if we are waiting for the connection signal
            if self.latency_waiting_for_connection:
                # Check if any line contains a latency measurement
                if re.search(r'\d+\.\d+\s+ms', data):
                    self.latency_waiting_for_connection = False
                    self.latency_results_text.setText("Connection detected. Running test...") # Changed message
                    # Start the timer now
                    self.latency_timer.setSingleShot(True)
                    self.latency_timer.timeout.connect(self.stop_latency_test)
                    self.latency_timer.start(10000) # 10 seconds

            # If not waiting (or connection just detected), parse for values
            if not self.latency_waiting_for_connection:
                for line in data.splitlines():
                    # Updated regex to capture both frames and ms
                    match = re.search(r'(\d+\.\d+)\s+frames\s+(\d+\.\d+)\s+ms', line)
                    if match:
                        try:
                            latency_frames = float(match.group(1))
                            latency_ms = float(match.group(2))
                            # Store both values as a tuple
                            self.latency_values.append((latency_frames, latency_ms))
                        except ValueError:
                            pass # Ignore lines that don't parse correctly

    def stop_latency_test(self):
        """Stops the jack_delay process."""
        if self.latency_timer.isActive():
            self.latency_timer.stop() # Stop timer if called manually before timeout

        if self.latency_process is not None and self.latency_process.state() != QProcess.ProcessState.NotRunning:
            self.latency_results_text.append("\nStopping test...")
            self.latency_process.terminate()
            # Give it a moment to terminate gracefully before potentially killing
            if not self.latency_process.waitForFinished(500):
                self.latency_process.kill()
                self.latency_process.waitForFinished() # Wait for kill confirmation

            self.latency_waiting_for_connection = False # Reset flag

    def handle_latency_finished(self, exit_code, exit_status):
        """Handles the jack_delay process finishing."""
        # Clear previous text before showing final result
        self.latency_results_text.clear()

        if self.latency_raw_output_checkbox.isChecked():
            # If raw output was shown, just indicate stop
            self.latency_results_text.setText("Measurement stopped.")
        elif self.latency_values:
            # Calculate average for frames and ms separately (only if not raw output)
            total_frames = sum(val[0] for val in self.latency_values)
            total_ms = sum(val[1] for val in self.latency_values)
            count = len(self.latency_values)
            average_frames = total_frames / count
            average_ms = total_ms / count
            # Display both average latencies
            self.latency_results_text.setText(f"Round-trip latency (average): {average_frames:.3f} frames / {average_ms:.3f} ms")
        else:
            # Check if the process exited normally but produced no values
            if exit_status == QProcess.ExitStatus.NormalExit and exit_code == 0:
                 # Display a clear error message
                 self.latency_results_text.setText("No valid latency readings obtained. Check connections.")
            elif exit_status == QProcess.ExitStatus.CrashExit:
                 self.latency_results_text.setText("Measurement stopped.")
            # Error message handled by handle_latency_error if exit code != 0 and no values were found
            elif exit_code != 0:
                 # If an error occurred (handled by handle_latency_error),
                 # ensure some message is shown if handle_latency_error didn't set one.
                 if not self.latency_results_text.toPlainText():
                     self.latency_results_text.setText(f"Test failed (Exit code: {exit_code}). Check connections.")
            else: # Should not happen often, but catch other cases
                 self.latency_results_text.setText("Test finished without valid readings.")


        self.latency_waiting_for_connection = False # Reset flag
        self.latency_run_button.setEnabled(True)
        self.latency_stop_button.setEnabled(False) # Disable Stop button
        self.latency_process = None # Clear the process reference

    def handle_latency_error(self, error):
        """Handles errors occurring during the jack_delay process execution."""
        error_string = self.latency_process.errorString() if self.latency_process else "Unknown error"
        self.latency_results_text.append(f"\nError running jack_delay: {error} - {error_string}")

        # Ensure timer and process are stopped/cleaned up
        if self.latency_timer.isActive():
            self.latency_timer.stop()
        if self.latency_process is not None:
            # Ensure process is terminated if it hasn't finished yet
            if self.latency_process.state() != QProcess.ProcessState.NotRunning:
                self.latency_process.kill()
                self.latency_process.waitForFinished()
            self.latency_process = None

        self.latency_waiting_for_connection = False # Reset flag
        self.latency_run_button.setEnabled(True)
        self.latency_stop_button.setEnabled(False) # Disable Stop button on error

    # --- End Latency Test Methods ---

    # --- Latency Tab UI Setup (Adapted from JackConnectionManager) ---
    def setup_latency_tab(self, layout): # Changed to accept layout directly
        """Set up the Latency Test tab"""
        # layout = QVBoxLayout(tab_widget) # Removed - use passed layout

        # Instructions Label
        instructions_text = (
            "<b>Instructions:</b><br><br>"
            "1. Ensure 'jack_delay', 'jack-delay' or 'jack_iodelay' (via 'jack-example-tools') is installed.<br>"
            "2. Physically connect an output and input of your audio interface using a cable (loopback).<br>"
            "3. Select the corresponding Input (Capture) and Output (Playback) ports using the dropdowns below.<br>"
            "4. Click 'Start Measurement'. The selected ports will be automatically connected to jack_delay.<br>"
            "(you can click 'Start Measurement' first and then try different ports)<br>"
            "5. <b><font color='orange'>Warning:</font></b> Start with low volume/gain levels on your interface "
            "to avoid potential damage from the test signal.<br><br>"
            "After the signal is detected, the average measured round-trip latency will be shown after 10 seconds.<br><br>" # Removed extra line breaks
        )

        instructions_label = QLabel(instructions_text)
        instructions_label.setWordWrap(True)
        instructions_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        # Increase font size for instructions
        instructions_label.setStyleSheet(f"color: {self.text_color.name()}; font-size: 11pt;")
        layout.addWidget(instructions_label)

        # --- Combo Boxes for Port Selection ---
        self.latency_input_combo = QComboBox()
        self.latency_input_combo.setPlaceholderText("Select Input (Capture)...")
        self.latency_input_combo.setStyleSheet(self.list_stylesheet()) # Reuse list style

        self.latency_output_combo = QComboBox()
        self.latency_output_combo.setPlaceholderText("Select Output (Playback)...")
        self.latency_output_combo.setStyleSheet(self.list_stylesheet()) # Reuse list style

        # --- Refresh Button ---
        self.latency_refresh_button = QPushButton("Refresh Ports")
        self.latency_refresh_button.setStyleSheet(self.button_stylesheet())
        self.latency_refresh_button.clicked.connect(self._populate_latency_combos) # Connect to refresh method
        # --- End Refresh Button ---

        # Input Row
        input_combo_layout = QHBoxLayout()
        input_combo_layout.addWidget(QLabel("Input Port:   "))
        input_combo_layout.addWidget(self.latency_input_combo, 1) # Let combo box expand
        # input_combo_layout.addStretch(1) # Removed spacer
        layout.addLayout(input_combo_layout)

        # Output Row
        output_combo_layout = QHBoxLayout()
        output_combo_layout.addWidget(QLabel("Output Port:"))
        output_combo_layout.addWidget(self.latency_output_combo, 1) # Let combo box expand
        # output_combo_layout.addStretch(1) # Removed spacer
        layout.addLayout(output_combo_layout)

        # Add Refresh button below, aligned with dropdowns
        refresh_button_layout = QHBoxLayout()
        # Add a label as a spacer, matching the longest label width ("Input  Port:  ")
        spacer_label = QLabel("Input  Port:  ")
        # Make the spacer label transparent so it doesn't draw text
        spacer_label.setStyleSheet("color: transparent;")
        refresh_button_layout.addWidget(spacer_label)
        refresh_button_layout.addWidget(self.latency_refresh_button, 1) # Button expands
        layout.addLayout(refresh_button_layout)
        # --- End Combo Boxes ---


        # Buttons Layout
        button_layout = QHBoxLayout()
        self.latency_run_button = QPushButton('Start measurement')
        self.latency_run_button.setStyleSheet(self.button_stylesheet())
        self.latency_run_button.clicked.connect(self.run_latency_test)

        self.latency_stop_button = QPushButton('Stop')
        self.latency_stop_button.setStyleSheet(self.button_stylesheet())
        self.latency_stop_button.clicked.connect(self.stop_latency_test)
        self.latency_stop_button.setEnabled(False) # Initially disabled

        button_layout.addWidget(self.latency_run_button)
        button_layout.addWidget(self.latency_stop_button)
        layout.addLayout(button_layout) # Add the horizontal layout for buttons

        # Raw Output Toggle Checkbox
        self.latency_raw_output_checkbox = QCheckBox("Show Raw Output (Continuous)")
        self.latency_raw_output_checkbox.setToolTip("If 'ON', measurement has to be stopped manually with 'Stop' button")
        self.latency_raw_output_checkbox.setStyleSheet(f"color: {self.text_color.name()};") # Style checkbox text

        # Results Text Edit
        self.latency_results_text = QTextEdit()
        self.latency_results_text.setReadOnly(True)
        # Increase font size for results text
        self.latency_results_text.setStyleSheet(f"""
            QTextEdit {{
                background-color: {self.background_color.name()};
                color: {self.text_color.name()};
                font-family: monospace;
                font-size: 14pt;
            }}
        """)
        self.latency_results_text.setText("Ready to test.")
        layout.addWidget(self.latency_results_text, 1) # Add stretch factor
        layout.addWidget(self.latency_raw_output_checkbox) # Add checkbox below results

        # Populate combo boxes
        self._populate_latency_combos()

        # Connect signals
        self.latency_input_combo.currentIndexChanged.connect(self._on_latency_input_selected)
        self.latency_output_combo.currentIndexChanged.connect(self._on_latency_output_selected)

    # --- Latency Port Selection/Connection Methods (Copied/Adapted) ---

    def _populate_latency_combos(self):
        """Populates the latency test combo boxes using python-jack."""
        capture_ports = [] # Physical capture devices (JACK outputs)
        playback_ports = [] # Physical playback devices (JACK inputs)
        try:
            # Get physical capture ports (System Output -> JACK Input)
            jack_capture_ports = self.client.get_ports(is_physical=True, is_audio=True, is_output=True)
            capture_ports = sorted([port.name for port in jack_capture_ports])

            # Get physical playback ports (System Input <- JACK Output)
            jack_playback_ports = self.client.get_ports(is_physical=True, is_audio=True, is_input=True)
            playback_ports = sorted([port.name for port in jack_playback_ports])

        except jack.JackError as e:
            print(f"Error getting physical JACK ports: {e}")
            # Optionally display an error in the UI
            self.latency_results_text.append(f"\nError getting JACK ports: {e}")


        # Block signals while populating to avoid triggering handlers prematurely
        self.latency_input_combo.blockSignals(True)
        self.latency_output_combo.blockSignals(True)

        # Clear existing items first, keeping placeholder
        self.latency_input_combo.clear()
        self.latency_output_combo.clear()
        self.latency_input_combo.addItem("Select Physical Input (Capture)...", None) # Add placeholder back
        self.latency_output_combo.addItem("Select Physical Output (Playback)...", None) # Add placeholder back

        # Populate Input Combo (Capture Ports - JACK Outputs)
        for port_name in capture_ports:
            self.latency_input_combo.addItem(port_name, port_name) # Use name for display and data

        # Populate Output Combo (Playback Ports - JACK Inputs)
        for port_name in playback_ports:
            self.latency_output_combo.addItem(port_name, port_name) # Use name for display and data

        # Restore previous selection if port names still exist
        if self.latency_selected_input_alias:
            index = self.latency_input_combo.findData(self.latency_selected_input_alias)
            if index != -1:
                self.latency_input_combo.setCurrentIndex(index)
        if self.latency_selected_output_alias:
            index = self.latency_output_combo.findData(self.latency_selected_output_alias)
            if index != -1:
                self.latency_output_combo.setCurrentIndex(index)

        # Unblock signals
        self.latency_input_combo.blockSignals(False)
        self.latency_output_combo.blockSignals(False)


    def _on_latency_input_selected(self, index):
        """Stores the selected physical input port alias."""
        self.latency_selected_input_alias = self.latency_input_combo.itemData(index)
        # Attempt connection if output is also selected and test is running
        self._attempt_latency_auto_connection()

    def _on_latency_output_selected(self, index):
        """Stores the selected physical output port alias."""
        self.latency_selected_output_alias = self.latency_output_combo.itemData(index)
        # Attempt connection if input is also selected and test is running
        self._attempt_latency_auto_connection()

    def _attempt_latency_auto_connection(self):
        """Connects selected physical ports to jack_delay if ports are selected."""
        # Only connect if both an input and output alias have been selected from the dropdowns.
        # The call to this function is now triggered by jack_delay port registration.
        if (self.latency_selected_input_alias and
            self.latency_selected_output_alias):

            # Pipewire 'in' direction (our output_ports list) connects to jack_delay:out
            # Pipewire 'out' direction (our input_ports list) connects to jack_delay:in
            output_to_connect = self.latency_selected_output_alias # This is the physical playback port alias
            input_to_connect = self.latency_selected_input_alias   # This is the physical capture port alias

            print(f"Attempting auto-connection: jack_delay:out -> {output_to_connect}")
            print(f"Attempting auto-connection: {input_to_connect} -> jack_delay:in")

            # Use the existing connection methods (ensure jack_delay ports exist first)
            # We might need a small delay or check if jack_delay ports are ready.
            # For now, let's assume they appear quickly after process start.
            try:
                # Connect jack_delay output to the selected physical playback port
                # Ensure the target port exists before connecting
                if any(p.name == output_to_connect for p in self.client.get_ports(is_input=True, is_audio=True)):
                     self.make_connection("jack_delay:out", output_to_connect)
                else:
                     print(f"Warning: Target output port '{output_to_connect}' not found.")

                # Connect the selected physical capture port to jack_delay input
                # Ensure the target port exists before connecting
                if any(p.name == input_to_connect for p in self.client.get_ports(is_output=True, is_audio=True)):
                    self.make_connection(input_to_connect, "jack_delay:in")
                else:
                    print(f"Warning: Target input port '{input_to_connect}' not found.")

                # self.latency_results_text.append("\nTry diffrent ports if you're seeing this message after clicking 'Start measurement button") # Maybe too noisy

            except jack.JackError as e:
                 # Catch specific Jack errors if needed, e.g., port not found
                 print(f"Error during latency auto-connection (JackError): {e}")
                 self.latency_results_text.append(f"\nError auto-connecting (JACK): {e}")
            except Exception as e:
                print(f"Error during latency auto-connection: {e}")
                self.latency_results_text.append(f"\nError auto-connecting: {e}")

    # --- Helper/Dependency Methods (Copied/Adapted) ---

    def make_connection(self, output_name, input_name):
        """Simplified connection method for audio ports."""
        try:
            # Check if connection already exists before attempting to connect
            try:
                connections = self.client.get_all_connections(output_name)
                if any(conn.name == input_name for conn in connections):
                    print(f"Connection {output_name} -> {input_name} already exists, skipping")
                    return
            except jack.JackError:
                # If we can't check connections, try the connect anyway
                pass

            self.client.connect(output_name, input_name)
            print(f"Connected: {output_name} -> {input_name}")

        except jack.JackError as e:
            print(f"Connect error: {e}")
            # Don't crash on connection errors, just log them

    def _handle_port_registration(self, port, register: bool):
        """JACK callback for port registration events. This runs in JACK's thread."""
        try:
            if port is None: return
            port_name = None
            is_input = False

            if hasattr(port, 'name'):
                try:
                    port_name = port.name
                    if not isinstance(port_name, str) or not port_name: return
                except Exception: return

            if hasattr(port, 'is_input'):
                try: is_input = port.is_input
                except Exception: is_input = False

            if port_name:
                if register:
                    self.port_registered.emit(port_name, is_input)
                # else: # Unregister signal not strictly needed here
                #     self.port_unregistered.emit(port_name, is_input)
        except Exception as e:
            print(f"Port registration callback error: {type(e).__name__}: {e}")

    def _on_port_registered(self, port_name: str, is_input: bool):
        """Handle port registration events in the Qt main thread"""
        if not self.callbacks_enabled: return

        # Check if this is a jack_delay port registration, and if so, attempt auto-connection
        if port_name == "jack_delay:in" or port_name == "jack_delay:out":
            print(f"Detected registration of {port_name}, attempting latency auto-connection...")
            # Use QTimer.singleShot to slightly delay the connection attempt,
            # ensuring both jack_delay ports might be ready.
            QTimer.singleShot(50, self._attempt_latency_auto_connection) # 50ms delay

        # Refresh the combo boxes if a physical port is registered/unregistered
        # Check if it's likely a physical port (often contains 'system:')
        if 'system:' in port_name or 'alsa_input' in port_name or 'alsa_output' in port_name:
             QTimer.singleShot(50, self._populate_latency_combos) # Refresh combos slightly delayed


    def is_dark_mode(self):
        palette = QApplication.palette()
        return palette.window().color().lightness() < 128

    def setup_colors(self):
        if self.dark_mode:
            self.background_color = QColor(53, 53, 53)
            self.text_color = QColor(255, 255, 255)
            self.highlight_color = QColor(42, 130, 218)
            self.button_color = QColor(68, 68, 68)
        else:
            self.background_color = QColor(255, 255, 255)
            self.text_color = QColor(0, 0, 0)
            self.highlight_color = QColor(173, 216, 230)
            self.button_color = QColor(240, 240, 240)

    def list_stylesheet(self):
        # Style for QComboBox to match connection-manager list/button styles
        return f"""
            QComboBox {{
                background-color: {self.button_color.name()}; /* Use button color for consistency */
                color: {self.text_color.name()};
                border: 1px solid {self.text_color.darker(120).name()}; /* Slightly darker border */
                padding: 3px;
                min-height: 20px; /* Ensure decent height */
            }}
            QComboBox:hover {{
                 border: 1px solid {self.highlight_color.name()}; /* Highlight border on hover */
            }}
            QComboBox::drop-down {{ /* Style the dropdown arrow area */
                border: none;
                background-color: {self.button_color.name()};
            }}
            QComboBox QAbstractItemView {{ /* Style for dropdown list */
                background-color: {self.background_color.name()};
                color: {self.text_color.name()};
                selection-background-color: {self.highlight_color.name()};
                border: 1px solid {self.text_color.darker(120).name()};
                padding: 2px;
            }}
        """

    def button_stylesheet(self):
        # Simplified to match connection-manager button style more closely
        return f"""
            QPushButton {{
                background-color: {self.button_color.name()};
                color: {self.text_color.name()};
                padding: 5px; /* Keep some padding */
                border: none; /* Remove border */
             }}
            QPushButton:hover {{ background-color: {self.highlight_color.name()}; }}
            QPushButton:disabled {{
                background-color: {self.background_color.darker(110).name()};
                color: {self.text_color.darker(150).name()};
            }}
        """

    def closeEvent(self, event):
        """Handle window closing behavior"""
        print("Closing Latency Tester...")
        # Stop latency test process before closing
        if hasattr(self, 'latency_process') and self.latency_process is not None:
            self.stop_latency_test()

        # Clean up JACK client and deactivate callbacks
        if hasattr(self, 'client'):
            try:
                self.callbacks_enabled = False
                self.client.deactivate()
                self.client.close()
                print("JACK client closed.")
            except jack.JackError as e:
                print(f"Error closing JACK client: {e}")

        event.accept()
        QApplication.quit()


def main():
    # Redirect stderr to /dev/null to suppress JACK callback errors (optional)
    # if not os.environ.get('DEBUG_JACK_CALLBACKS'):
    #     sys.stderr = open(os.devnull, 'w')

    app = QApplication(sys.argv)
    # Set a unique desktop filename if needed for icons/taskbar
    QGuiApplication.setDesktopFileName("com.example.latency")
   # self.setApplicationName("latency")
    window = LatencyTesterApp()
    window.show()

    # Handle Ctrl+C gracefully
    import signal
    def signal_handler(signum, frame):
        print("Received signal to terminate")
        window.close() # Trigger closeEvent for cleanup
        # app.quit() # closeEvent handles quit
        # sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    sys.exit(app.exec())


if __name__ == '__main__':
    main()
