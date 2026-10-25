"""Small macOS Keychain adapter; secret values never enter argv or JSON files."""
import argparse
import ctypes
import getpass
import sys


class KeychainError(RuntimeError):
    pass


def _frameworks():
    if sys.platform != 'darwin':
        raise KeychainError('macOS Keychain is unavailable on this platform')
    cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    sec = ctypes.CDLL('/System/Library/Frameworks/Security.framework/Security')
    ptr = ctypes.c_void_p
    cf.CFStringCreateWithCString.argtypes = [ptr, ctypes.c_char_p, ctypes.c_uint32]
    cf.CFStringCreateWithCString.restype = ptr
    cf.CFDataCreate.argtypes = [ptr, ptr, ctypes.c_long]
    cf.CFDataCreate.restype = ptr
    cf.CFDictionaryCreate.argtypes = [ptr, ctypes.POINTER(ptr), ctypes.POINTER(ptr), ctypes.c_long, ptr, ptr]
    cf.CFDictionaryCreate.restype = ptr
    cf.CFDataGetLength.argtypes = [ptr]
    cf.CFDataGetLength.restype = ctypes.c_long
    cf.CFDataGetBytePtr.argtypes = [ptr]
    cf.CFDataGetBytePtr.restype = ptr
    cf.CFRelease.argtypes = [ptr]
    sec.SecItemAdd.argtypes = [ptr, ctypes.POINTER(ptr)]
    sec.SecItemAdd.restype = ctypes.c_int32
    sec.SecItemUpdate.argtypes = [ptr, ptr]
    sec.SecItemUpdate.restype = ctypes.c_int32
    sec.SecItemCopyMatching.argtypes = [ptr, ctypes.POINTER(ptr)]
    sec.SecItemCopyMatching.restype = ctypes.c_int32
    return cf, sec


def _operate(service, account, value=None):
    if not service or not account:
        raise KeychainError('Keychain service and account are required')
    cf, sec = _frameworks()
    ptr = ctypes.c_void_p
    owned = []

    def constant(name, lib=sec):
        return ptr.in_dll(lib, name).value

    def string(text):
        obj = cf.CFStringCreateWithCString(None, text.encode('utf-8'), 0x08000100)
        owned.append(obj)
        return obj

    def dictionary(pairs):
        keys = (ptr * len(pairs))(*(k for k, _ in pairs))
        vals = (ptr * len(pairs))(*(v for _, v in pairs))
        obj = cf.CFDictionaryCreate(None, keys, vals, len(pairs), None, None)
        owned.append(obj)
        return obj

    pairs = [(constant('kSecClass'), constant('kSecClassGenericPassword')),
             (constant('kSecAttrService'), string(service)),
             (constant('kSecAttrAccount'), string(account))]
    try:
        if value is not None:
            if not isinstance(value, bytes) or not value or len(value) > 65536:
                raise KeychainError('Secret must contain 1 to 65536 bytes')
            data = cf.CFDataCreate(None, ctypes.c_char_p(value), len(value))
            owned.append(data)
            query = dictionary(pairs)
            attrs = dictionary([(constant('kSecValueData'), data)])
            status = sec.SecItemUpdate(query, attrs)
            if status == -25300:
                status = sec.SecItemAdd(dictionary(pairs + [(constant('kSecValueData'), data)]), None)
            if status:
                raise KeychainError('Keychain write failed (status %d)' % status)
            return None
        result = ptr()
        query = dictionary(pairs + [(constant('kSecReturnData'), constant('kCFBooleanTrue', cf)),
                                    (constant('kSecMatchLimit'), constant('kSecMatchLimitOne')),
                                    (constant('kSecUseAuthenticationUI'), constant('kSecUseAuthenticationUIFail'))])
        status = sec.SecItemCopyMatching(query, ctypes.byref(result))
        if status:
            raise KeychainError('Keychain read unavailable (status %d)' % status)
        owned.append(result.value)
        return ctypes.string_at(cf.CFDataGetBytePtr(result), cf.CFDataGetLength(result))
    finally:
        for obj in reversed(owned):
            if obj:
                cf.CFRelease(obj)


def put(service, account, value):
    _operate(service, account, value)


def get(service, account):
    return _operate(service, account)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Save a secret from stdin to macOS Keychain; never print its value')
    parser.add_argument('action', choices=['put'])
    parser.add_argument('--service', required=True)
    parser.add_argument('--account', required=True)
    args = parser.parse_args()
    try:
        value = getpass.getpass('Secret: ').encode() if sys.stdin.isatty() else sys.stdin.buffer.read().rstrip(b'\r\n')
        put(args.service, args.account, value)
        print('Saved to macOS Keychain')
    except KeychainError as error:
        parser.exit(1, str(error) + '\n')
