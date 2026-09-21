@echo off
cd /d "%~dp0"
echo Iniciando la app de bloqueo de choferes...
echo (si la app ya estaba corriendo en otra ventana, cerrala antes para que tome los cambios)
echo.
python app.py
echo.
echo La app se detuvo o hubo un error. Revisa el mensaje de arriba.
pause
