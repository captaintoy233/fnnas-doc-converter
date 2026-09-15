"""
结构化异常体系测试

验证各异常类型的正确性和 error_code 属性。
"""
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app', 'server'))

from errors import (
    ConversionError,
    UnsupportedFormatError,
    NeedsOcrError,
    MalformedDocumentError,
    EncryptedDocumentError,
    ResourceLimitError,
    MissingPartError,
)


class TestConversionErrors:
    """异常体系测试"""
    
    def test_base_error(self):
        err = ConversionError("test error", file_path="/path/to/file.doc")
        assert str(err) == "test error"
        assert err.file_path == "/path/to/file.doc"
        assert err.error_code == "conversion_error"
    
    def test_unsupported_format(self):
        err = UnsupportedFormatError("Unknown format", format_name="xyz")
        assert err.format_name == "xyz"
        assert err.error_code == "unsupported_format"
    
    def test_needs_ocr(self):
        err = NeedsOcrError(pages=[0, 2, 5])
        assert err.pages == [0, 2, 5]
        assert err.page_count == 3
        assert err.error_code == "needs_ocr"
    
    def test_needs_ocr_empty(self):
        err = NeedsOcrError()
        assert err.pages == []
        assert err.page_count == 0
    
    def test_malformed_document(self):
        err = MalformedDocumentError("Corrupt structure")
        assert err.error_code == "malformed_document"
    
    def test_encrypted_document(self):
        err = EncryptedDocumentError()
        assert err.error_code == "encrypted_document"
    
    def test_resource_limit(self):
        err = ResourceLimitError(
            "Decompression too large",
            limit_type="decompression_size",
            limit_value=1024 * 1024 * 100
        )
        assert err.limit_type == "decompression_size"
        assert err.limit_value == 1024 * 1024 * 100
        assert err.error_code == "resource_limit"
    
    def test_missing_part(self):
        err = MissingPartError(part_name="word/document.xml")
        assert err.part_name == "word/document.xml"
        assert err.error_code == "missing_part"
    
    def test_exception_hierarchy(self):
        """所有异常都继承自 ConversionError"""
        assert issubclass(UnsupportedFormatError, ConversionError)
        assert issubclass(NeedsOcrError, ConversionError)
        assert issubclass(MalformedDocumentError, ConversionError)
        assert issubclass(EncryptedDocumentError, ConversionError)
        assert issubclass(ResourceLimitError, ConversionError)
        assert issubclass(MissingPartError, ConversionError)
    
    def test_catch_base_class(self):
        """可以用基类捕获所有转换错误"""
        errors = [
            UnsupportedFormatError("test"),
            NeedsOcrError(),
            EncryptedDocumentError(),
        ]
        
        caught = 0
        for err in errors:
            try:
                raise err
            except ConversionError:
                caught += 1
        
        assert caught == 3
