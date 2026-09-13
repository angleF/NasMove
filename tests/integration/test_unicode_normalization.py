import unicodedata

def test_nfd_to_nfc_conversion():
    # 'e' + '´' (NFD)
    nfd_name = "re\u0301sume\u0301.txt"
    # 'é' (NFC)
    nfc_name = "résumé.txt"

    assert unicodedata.is_normalized("NFD", nfd_name)
    assert unicodedata.is_normalized("NFC", nfc_name)
    assert nfd_name != nfc_name

    def normalize_for_smb(name: str) -> str:
        return unicodedata.normalize("NFC", name).casefold()

    # The NFD name from macOS should match the NFC name on the SMB share
    assert normalize_for_smb(nfd_name) == normalize_for_smb(nfc_name)
