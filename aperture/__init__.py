"""
A local chat terminal for a 20x4 HD44780 character display on a Raspberry Pi.

The program is layered so each part can be tested without the one below it:

    aperture.hal        panel driver, framebuffer, keyboards -- and an
                        emulator faithful enough to stand in for the panel
    aperture.llm        llama.cpp process supervision and streaming client
    aperture.services   Wi-Fi, Bluetooth and host telemetry
    aperture.ui         screens, widgets and the render loop
    aperture.config     the typed settings schema both the validator and the
                        settings menu are generated from
"""

VERSION = "1.0.0"
BUILD_NAME = "APERTURE TERMINAL"
