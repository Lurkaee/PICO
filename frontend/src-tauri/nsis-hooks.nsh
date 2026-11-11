!macro NSIS_HOOK_POSTUNINSTALL
  ; Remove only PICO's explicit HKCU Run auto-start value during genuine uninstallation.
  ; Preserve the Run value during in-place upgrades ($UpdateMode = 1), and preserve the
  ; Run registry key, unrelated startup entries, and per-user data in %LOCALAPPDATA%\PICO.
  ${If} $UpdateMode <> 1
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "PICO"
    ClearErrors
  ${EndIf}
!macroend
