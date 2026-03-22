"""
Blueprint serving Swagger UI and the OpenAPI 3.0 JSON spec.

- GET /api/docs         → Swagger UI HTML page (CDN-hosted)
- GET /api/docs/openapi.json → OpenAPI 3.0 spec as JSON
"""

from flask import Blueprint, jsonify

api_docs_bp = Blueprint('api_docs', __name__, url_prefix='/api/docs')


@api_docs_bp.route('')
@api_docs_bp.route('/')
def swagger_ui():
    """Serve a self-contained Swagger UI page loaded from CDN."""
    html = _SWAGGER_HTML.replace('{{SPEC_URL}}', '/api/docs/openapi.json')
    return html, 200, {'Content-Type': 'text/html; charset=utf-8'}


@api_docs_bp.route('/openapi.json')
def openapi_json():
    """Return the OpenAPI 3.0 specification."""
    from openapi_spec import get_openapi_spec
    return jsonify(get_openapi_spec())


_SWAGGER_HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>EthOS API Documentation</title>
  <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
  <style>
    html { box-sizing: border-box; overflow-y: scroll; }
    *, *::before, *::after { box-sizing: inherit; }
    body { margin: 0; background: #fafafa; }
    .swagger-ui .topbar { display: none; }
    .swagger-ui .info hgroup.main a { display: none; }
  </style>
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>
    SwaggerUIBundle({
      url: '{{SPEC_URL}}',
      dom_id: '#swagger-ui',
      presets: [
        SwaggerUIBundle.presets.apis,
        SwaggerUIBundle.SwaggerUIStandalonePreset
      ],
      layout: 'BaseLayout',
      deepLinking: true,
      defaultModelsExpandDepth: 1,
      defaultModelExpandDepth: 1,
      docExpansion: 'list',
      filter: true,
      showExtensions: true,
      showCommonExtensions: true,
      persistAuthorization: true,
    });
  </script>
</body>
</html>
'''
