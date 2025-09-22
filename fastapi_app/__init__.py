"""
The ``fastapi_app`` package contains the main FastAPI application along with
database configuration, models, routes and service integrations.  This
package is structured to keep the project modular and maintainable.

The application is split into several subpackages:

* :mod:`fastapi_app.db` – database engine and session handling
* :mod:`fastapi_app.models` – SQLAlchemy model definitions
* :mod:`fastapi_app.routes` – API route handlers (controllers)
* :mod:`fastapi_app.services` – helper functions for interacting with
  external services such as the OpenAI Assistants API and Uazapi
* :mod:`fastapi_app.migrations` – SQL migration scripts

Importing :class:`fastapi_app.main.app` gives you the configured
FastAPI application instance.
"""
