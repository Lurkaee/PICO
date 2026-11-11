!macro NSIS_HOOK_POSTUNINSTALL
  ; Remove only PICO's explicit HKCU Run auto-start value during uninstall.
  ; Preserves the Run registry key, unrelated startup entries, and per-user data in %LOCALAPPDATA%\PICO.
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "PICO"
  ClearErrors
!macroend
