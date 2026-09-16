idf_component_register(
    SRCS "main.c"
         "radar_task.c"
         "mmwave_frame.c"
         "mr60bha2.c"
    INCLUDE_DIRS "."
    REQUIRES driver esp_app_format
    PRIV_REQUIRES hal soc
)
