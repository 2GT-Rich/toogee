# Wili: the FreeWili OG as a voice-assistant head for a host computer.
# Display CPU: animated face (core 1), mic + speaker streaming over USB,
# buttons and LEDs (core 0). Main CPU: the template's watchdog + display
# bring-up. Protocol: display/wili_proto.h; host side: wili_board.py.
add_executable(wili_display display/main.c display/face.c)
target_link_libraries(wili_display fwog_display_bsp pico_multicore)
# The SDK's 64-byte CDC FIFOs can't carry two 16 KB/s audio streams beside a
# busy main loop. tusb_config.h only sets these #ifndef, so this target's
# TinyUSB build picks them up.
target_compile_definitions(wili_display PRIVATE
    CFG_TUD_CDC_RX_BUFSIZE=4096
    CFG_TUD_CDC_TX_BUFSIZE=4096)
fwog_display_app(wili_display
    VERSION 007
    DESCRIPTION "Wili voice assistant head: animated face, USB mic/speaker streaming, buttons, mood LEDs")

add_executable(wili_main main/main.c)
target_link_libraries(wili_main fwog_main_bsp)
# Carries wili_display inside this UF2 (no argument = this folder's display).
fwog_embed_display_image(wili_main)
fwog_main_app(wili_main
    VERSION 007
    DESCRIPTION "Wili main CPU: brings the Wili display firmware up and keeps the watchdog fed")
