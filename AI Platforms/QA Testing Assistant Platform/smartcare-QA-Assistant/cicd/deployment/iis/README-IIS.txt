SmartCare QA Assistant - IIS Hosting Notes

This project is a FastAPI (ASGI) application and is hosted on IIS using HttpPlatformHandler.

Required IIS components
- IIS Web Server role
- HttpPlatformHandler module

Site setup steps
1. Extract artifact to a folder, for example: C:\Apps\smartcare-qa-assistant
2. Run installation from package root:
   powershell -ExecutionPolicy Bypass -File deployment\Install-SmartCareQA.ps1 -InstallLocalPresidioModel
3. Copy deployment\SmartCareQA.env.template to .env and fill environment values.
4. Copy deployment\iis\web.config.httpplatform.template to web.config in package root.
5. Edit web.config and set processPath to your Python executable path.
6. In IIS, create a site/app pointing to this package root.
7. Grant Read/Execute permissions to the IIS app pool identity on package folder.
8. Recycle app pool and browse site URL.

Troubleshooting
- 500.19/500.0 at startup often means HttpPlatformHandler missing or wrong processPath.
- App startup failures: check stdout log file path configured in web.config.
- If running behind TLS termination, validate forwarded headers behavior at reverse proxy layer.
