"""
EthOS Standard Error Handling and Logging Module

This module provides standardized error handling and logging patterns for all blueprints
to improve consistency and AI readability.
"""

import logging
import traceback
from flask import jsonify
from datetime import datetime

# Setup standard logger
logger = logging.getLogger('ethos')

def standard_error_response(message, status_code=500, error_code=None):
    """
    Standard error response format for all blueprints.

    Args:
        message (str): Error message to return
        status_code (int): HTTP status code (default: 500)
        error_code (str): Optional error code for client-side handling

    Returns:
        tuple: (json_response, status_code)
    """
    response = {'error': message}
    if error_code:
        response['code'] = error_code
    return jsonify(response), status_code

def log_error(message, exc=None, extra_data=None):
    """
    Standard error logging function.

    Args:
        message (str): Error message
        exc (Exception): Optional exception object
        extra_data (dict): Optional additional data to log
    """
    if exc:
        logger.error(f"{message}: {exc}")
        if extra_data:
            logger.error(f"Extra data: {extra_data}")
        logger.error(f"Traceback: {traceback.format_exc()}")
    else:
        logger.error(message)
        if extra_data:
            logger.error(f"Extra data: {extra_data}")

def log_info(message, extra_data=None):
    """
    Standard info logging function.

    Args:
        message (str): Info message
        extra_data (dict): Optional additional data to log
    """
    logger.info(message)
    if extra_data:
        logger.info(f"Extra data: {extra_data}")

def log_warning(message, extra_data=None):
    """
    Standard warning logging function.

    Args:
        message (str): Warning message
        extra_data (dict): Optional additional data to log
    """
    logger.warning(message)
    if extra_data:
        logger.warning(f"Extra data: {extra_data}")

def standard_api_error(message, status_code=500, error_code=None, exc=None, extra_data=None):
    """
    Complete standard API error handling with logging.

    Args:
        message (str): Error message to return
        status_code (int): HTTP status code
        error_code (str): Optional error code for client-side handling
        exc (Exception): Optional exception object for logging
        extra_data (dict): Optional additional data to log

    Returns:
        tuple: (json_response, status_code)
    """
    # Log the error
    log_error(message, exc, extra_data)

    # Return standardized response
    response = {'error': message}
    if error_code:
        response['code'] = error_code
    return jsonify(response), status_code