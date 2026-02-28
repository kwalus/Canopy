"""
Comprehensive logging configuration for Canopy debugging.

Provides detailed logging across all components with different levels
and formatters for development and production use.

Author: Konrad Walus (architecture, design, and direction)
Project: Canopy - Local Mesh Communication
License: Apache 2.0
Development: AI-assisted implementation (Claude, Codex, GitHub Copilot, Cursor IDE, Ollama)
"""

import io
import logging
import logging.handlers
import sys
import os
import time
from pathlib import Path
from datetime import datetime
from typing import Any, Callable, Optional, cast


class WindowsSafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """RotatingFileHandler that tolerates Windows PermissionError on rollover (file in use)."""

    def doRollover(self):
        if self.stream:
            self.stream.close()
            self.stream = cast(Any, None)
        if self.backupCount > 0:
            for i in range(self.backupCount - 1, 0, -1):
                sfn = self.rotation_filename(self.baseFilename + "." + str(i))
                dfn = self.rotation_filename(self.baseFilename + "." + str(i + 1))
                if os.path.exists(sfn):
                    if os.path.exists(dfn):
                        os.remove(dfn)
                    try:
                        os.rename(sfn, dfn)
                    except OSError as e:
                        if sys.platform == "win32" and (getattr(e, "winerror", None) == 32 or getattr(e, "errno", None) in (13, 32)):
                            pass  # skip: file in use (ERROR_SHARING_VIOLATION / EACCES)
                        else:
                            raise
            dfn = self.rotation_filename(self.baseFilename + ".1")
            try:
                os.rename(self.baseFilename, dfn)
            except OSError as e:
                if sys.platform == "win32" and (getattr(e, "winerror", None) == 32 or getattr(e, "errno", None) in (13, 32)):
                    # File in use: retry once after brief delay
                    time.sleep(0.2)
                    try:
                        os.rename(self.baseFilename, dfn)
                    except OSError:
                        pass  # skip rotation this time
                else:
                    raise
        if not self.delay:
            self.stream = self._open()


def _safe_console_stream():
    """Return a stream that safely handles Unicode (e.g. emojis) on Windows cp1252."""
    stream = sys.stdout
    if hasattr(stream, 'buffer'):
        return io.TextIOWrapper(stream.buffer, encoding='utf-8', errors='replace', line_buffering=True)
    return stream


class ColoredFormatter(logging.Formatter):
    """Colored log formatter for console output."""
    
    COLORS = {
        'DEBUG': '\033[36m',      # Cyan
        'INFO': '\033[32m',       # Green  
        'WARNING': '\033[33m',    # Yellow
        'ERROR': '\033[31m',      # Red
        'CRITICAL': '\033[35m',   # Magenta
        'RESET': '\033[0m'        # Reset
    }
    
    def format(self, record):
        log_color = self.COLORS.get(record.levelname, self.COLORS['RESET'])
        reset_color = self.COLORS['RESET']
        
        # Add color to levelname
        record.levelname = f"{log_color}{record.levelname}{reset_color}"
        
        return super().format(record)


class CanopyLogger:
    """Centralized logging configuration for Canopy."""
    
    def __init__(self, debug: bool = False, log_dir: str = "logs"):
        self.debug = debug
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)
        
        # Clear any existing handlers
        logging.getLogger().handlers.clear()
        
        self._setup_loggers()
    
    def _setup_loggers(self):
        """Set up all loggers with appropriate handlers and formatters."""
        
        # Root logger
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG if self.debug else logging.INFO)
        
        # Create formatters
        detailed_formatter = logging.Formatter(
            '%(asctime)s | %(name)-20s | %(levelname)-8s | %(filename)s:%(lineno)d | %(funcName)s() | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        simple_formatter = logging.Formatter(
            '%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
            datefmt='%H:%M:%S'
        )
        
        colored_formatter = ColoredFormatter(
            '%(asctime)s | %(levelname)-8s | %(name)-15s | %(message)s',
            datefmt='%H:%M:%S'
        )
        
        # Console handler with colors (safe stream for Windows cp1252 + emojis)
        console_handler = logging.StreamHandler(_safe_console_stream())
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(colored_formatter)
        root_logger.addHandler(console_handler)
        
        # Main application log file (rotating; Windows-safe to avoid PermissionError on rollover)
        main_handler = WindowsSafeRotatingFileHandler(
            self.log_dir / "canopy.log",
            maxBytes=10*1024*1024,  # 10MB
            backupCount=5
        )
        main_handler.setLevel(logging.DEBUG if self.debug else logging.INFO)
        main_handler.setFormatter(detailed_formatter)
        root_logger.addHandler(main_handler)
        
        # Error log file (errors only)
        error_handler = WindowsSafeRotatingFileHandler(
            self.log_dir / "errors.log",
            maxBytes=5*1024*1024,   # 5MB
            backupCount=3
        )
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(detailed_formatter)
        root_logger.addHandler(error_handler)
        
        # Debug log file (everything, only in debug mode)
        if self.debug:
            debug_handler = WindowsSafeRotatingFileHandler(
                self.log_dir / "debug.log",
                maxBytes=20*1024*1024,  # 20MB
                backupCount=2
            )
            debug_handler.setLevel(logging.DEBUG)
            debug_handler.setFormatter(detailed_formatter)
            root_logger.addHandler(debug_handler)
        
        # Performance log file
        perf_handler = WindowsSafeRotatingFileHandler(
            self.log_dir / "performance.log",
            maxBytes=5*1024*1024,   # 5MB
            backupCount=2
        )
        perf_handler.setLevel(logging.INFO)
        perf_handler.setFormatter(simple_formatter)
        
        # Set up specific loggers
        self._setup_component_loggers(perf_handler)
        
        # Log startup with build stamp for cross-machine verification
        import hashlib as _hl, pathlib as _pl
        _build_stamp = "unknown"
        try:
            _mgr_path = _pl.Path(__file__).parent.parent / "network" / "manager.py"
            if _mgr_path.exists():
                _h = _hl.sha256(_mgr_path.read_bytes()).hexdigest()[:12]
                _build_stamp = f"mgr-{_h}"
        except Exception:
            pass
        from canopy import __version__ as _ver
        logging.info("=" * 60)
        logging.info(f"Canopy v{_ver}  build={_build_stamp}")
        logging.info(f"Debug mode: {self.debug}")
        logging.info(f"Log directory: {self.log_dir.absolute()}")
        logging.info("=" * 60)
    
    def _setup_component_loggers(self, perf_handler):
        """Set up loggers for specific Canopy components."""
        
        # Database operations
        db_logger = logging.getLogger('canopy.database')
        db_logger.setLevel(logging.DEBUG if self.debug else logging.INFO)
        
        # API operations
        api_logger = logging.getLogger('canopy.api')
        api_logger.setLevel(logging.DEBUG if self.debug else logging.INFO)
        
        # Security operations
        security_logger = logging.getLogger('canopy.security')
        security_logger.setLevel(logging.DEBUG if self.debug else logging.INFO)
        
        # Network operations
        network_logger = logging.getLogger('canopy.network')
        network_logger.setLevel(logging.DEBUG if self.debug else logging.INFO)
        
        # UI operations
        ui_logger = logging.getLogger('canopy.ui')
        ui_logger.setLevel(logging.DEBUG if self.debug else logging.INFO)
        
        # Performance logger
        perf_logger = logging.getLogger('canopy.performance')
        perf_logger.addHandler(perf_handler)
        perf_logger.setLevel(logging.INFO)
        perf_logger.propagate = False  # Don't propagate to root logger
        
        # Flask request logging
        flask_logger = logging.getLogger('werkzeug')
        flask_logger.setLevel(logging.WARNING)  # Reduce Flask noise
    
    @staticmethod
    def get_logger(name: str) -> logging.Logger:
        """Get a logger for a specific component."""
        return logging.getLogger(f'canopy.{name}')


# Performance measurement decorator
def log_performance(logger_name: str = 'performance') -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator to log function execution time."""
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            logger = logging.getLogger(f'canopy.{logger_name}')
            start_time = datetime.now()
            
            try:
                result = func(*args, **kwargs)
                execution_time = (datetime.now() - start_time).total_seconds()
                logger.debug(f"{func.__name__} executed in {execution_time:.3f}s")
                return result
            except Exception as e:
                execution_time = (datetime.now() - start_time).total_seconds()
                logger.error(f"{func.__name__} failed after {execution_time:.3f}s: {e}")
                raise
                
        return wrapper
    return decorator


# Context manager for logging operations
class LogOperation:
    """Context manager for logging operations with timing."""
    
    def __init__(self, operation_name: str, logger_name: str = 'performance'):
        self.operation_name = operation_name
        self.logger = logging.getLogger(f'canopy.{logger_name}')
        self.start_time: Optional[datetime] = None
        
    def __enter__(self):
        self.start_time = datetime.now()
        self.logger.debug(f"Starting {self.operation_name}")
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.start_time is None:
            return
        execution_time = (datetime.now() - self.start_time).total_seconds()
        
        if exc_type is None:
            self.logger.debug(f"Completed {self.operation_name} in {execution_time:.3f}s")
        else:
            self.logger.error(f"Failed {self.operation_name} after {execution_time:.3f}s: {exc_val}")


# Global function to initialize logging
def setup_logging(debug: bool = False, log_dir: str = "logs") -> CanopyLogger:
    """Initialize Canopy logging system."""
    return CanopyLogger(debug=debug, log_dir=log_dir)
