from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.models import (
    ContentMatch,
    FileOperationRequest,
    FileOperationResponse,
    FileSystemItem,
    Operation,
    Status,
)


REQUEST_NULLABLE_SCALARS = {
    'path',
    'destinationPath',
    'content',
    'searchPattern',
    'searchText',
    'replacementText',
    'fileExtension',
    'namePattern',
    'expectedHash',
    'expectedLastModifiedUtc',
    'expectedOccurrences',
    'shellCommand',
    'reason',
    'correlationId',
}
RESPONSE_NULLABLE_SCALARS = {
    'correlationId',
    'workspace',
    'path',
    'destinationPath',
    'errorCode',
    'errorMessage',
    'exists',
    'itemType',
    'content',
    'encoding',
    'name',
    'extension',
    'sizeBytes',
    'createdUtc',
    'modifiedUtc',
    'hash',
    'nextSkip',
    'exitCode',
    'recycleId',
    'recyclePath',
    'policyRule',
}


def load_swagger() -> dict[str, object]:
    path = Path('swagger/api-definition.swagger.yaml')
    return yaml.safe_load(path.read_text(encoding='utf-8'))


def test_swagger_defines_exactly_one_connector_operation() -> None:
    document = load_swagger()

    assert document['swagger'] == '2.0'
    assert set(document['paths']) == {'/filesystem/execute'}
    operation = document['paths']['/filesystem/execute']['post']
    assert operation['operationId'] == 'ExecuteWorkspaceFileOperation'
    assert document.get('securityDefinitions') is None


def test_mcp_swagger_declares_streamable_endpoint_for_copilot_studio() -> None:
    path = Path('swagger/mcp-streamable.swagger.yaml')
    document = yaml.safe_load(path.read_text(encoding='utf-8'))

    assert document['swagger'] == '2.0'
    assert document['basePath'] == '/'
    assert set(document['paths']) == {'/mcp'}
    operation = document['paths']['/mcp']['post']
    assert operation['operationId'] == 'InvokeMCP'
    assert operation['x-ms-agentic-protocol'] == 'mcp-streamable-1.0'
    assert operation['parameters'] == [
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {'$ref': '#/definitions/McpJsonRpcRequest'},
        }
    ]
    assert set(operation['responses']) == {'200'}
    assert operation['responses']['200']['schema'] == {
        '$ref': '#/definitions/McpJsonRpcResponse'
    }
    assert set(document['definitions']) == {
        'ContentMatch',
        'ExecuteWorkspaceFileOperationToolInput',
        'FileOperationRequest',
        'FileOperationResponse',
        'FileSystemItem',
        'McpContent',
        'McpJsonRpcError',
        'McpJsonRpcRequest',
        'McpJsonRpcResponse',
        'McpJsonRpcResult',
        'McpTool',
        'McpToolCallParams',
    }


def test_swagger_request_properties_exactly_match_pydantic() -> None:
    request = load_swagger()['definitions']['FileOperationRequest']

    assert request['additionalProperties'] is False
    assert set(request['required']) == {'operation', 'workspace'}
    assert set(request['properties']) == set(FileOperationRequest.model_fields)
    assert set(request['properties']['operation']['enum']) == {
        value.value for value in Operation
    }
    assert 'script' not in request['properties']


def test_request_bounds_and_hash_pattern_match_runtime_validation() -> None:
    properties = load_swagger()['definitions']['FileOperationRequest'][
        'properties'
    ]

    assert properties['content']['maxLength'] == 1000000
    assert properties['searchText']['maxLength'] == 10000
    assert properties['replacementText']['maxLength'] == 1000000
    assert properties['shellArguments']['items']['maxLength'] == 1000
    assert properties['expectedHash']['minLength'] == 71
    assert properties['expectedHash']['maxLength'] == 71
    assert properties['expectedHash']['pattern'] == (
        '^sha256:[0-9A-Fa-f]{64}$'
    )

    valid = FileOperationRequest(
        operation='UPDATE_FILE',
        workspace='test',
        expectedHash='sha256:' + ('A0' * 32),
    )
    assert valid.expectedHash == 'sha256:' + ('A0' * 32)

    for invalid in ('', '0' * 64, 'sha256:' + ('g' * 64)):
        with pytest.raises(ValidationError):
            FileOperationRequest(
                operation='UPDATE_FILE',
                workspace='test',
                expectedHash=invalid,
            )


def test_nullable_scalar_properties_are_explicit_in_swagger() -> None:
    definitions = load_swagger()['definitions']

    request = definitions['FileOperationRequest']['properties']
    assert {
        name for name, schema in request.items() if schema.get('x-nullable')
    } == REQUEST_NULLABLE_SCALARS

    response = definitions['FileOperationResponse']['properties']
    assert {
        name for name, schema in response.items() if schema.get('x-nullable')
    } == RESPONSE_NULLABLE_SCALARS

    item = definitions['FileSystemItem']['properties']
    assert {
        name for name, schema in item.items() if schema.get('x-nullable')
    } == {
        'extension',
        'sizeBytes',
        'createdUtc',
        'modifiedUtc',
        'hash',
    }

    match = definitions['ContentMatch']['properties']
    assert {
        name for name, schema in match.items() if schema.get('x-nullable')
    } == {'beforeText', 'afterText'}


@pytest.mark.parametrize(
    ('definition_name', 'model'),
    [
        ('FileOperationResponse', FileOperationResponse),
        ('FileSystemItem', FileSystemItem),
        ('ContentMatch', ContentMatch),
    ],
)
def test_always_serialized_properties_are_required_and_exact(
    definition_name: str,
    model: type,
) -> None:
    definition = load_swagger()['definitions'][definition_name]
    expected = set(model.model_fields)

    assert definition['additionalProperties'] is False
    assert set(definition['properties']) == expected
    assert set(definition['required']) == expected


def test_fixed_response_model_serializes_every_declared_property() -> None:
    response = FileOperationResponse(
        success=False,
        status=Status.FAILED,
        operation='UNKNOWN',
        operationId='operation-id',
        message='Rejected.',
    )

    assert set(response.model_dump(mode='json')) == set(
        FileOperationResponse.model_fields
    )


def test_swagger_declares_stable_response_for_every_status() -> None:
    document = load_swagger()
    responses = document['paths']['/filesystem/execute']['post']['responses']

    assert set(responses) == {
        '200',
        '400',
        '403',
        '404',
        '408',
        '409',
        '413',
        '422',
        '500',
    }
    for response in responses.values():
        assert response['schema'] == {
            '$ref': '#/definitions/FileOperationResponse'
        }
