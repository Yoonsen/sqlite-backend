from pathlib import Path
import xml.etree.ElementTree as ET
from nb_tokenizer import tokenize


def extract_tokens(xml_path: str):
    """
    Parse en TEI-XML fra bokselskap.no og returner en liste med tokens.

    Output-felt:
        book_id: filnavn uten suffiks
        seq: 0-indeksert løpenummer per bok
        raw: originalt token (string)
        cf: case-foldet token
        page: løpende side (oppdatert på <pb/>)
        para: løpende avsnitt/linje (p/l/ab/seg)
    """
    path = Path(xml_path)
    book_id = path.stem
    root = ET.parse(path).getroot()
    body = root.find(".//{*}body")
    if body is None:
        return []

    tokens = []
    seq = 0
    page = 0
    para = 0

    for elem in body.iter():
        tag = elem.tag
        if tag.endswith("pb"):
            page += 1
        if tag.endswith(("p", "l", "ab", "seg")):
            txt = "".join(elem.itertext()).strip()
            if not txt:
                continue
            para += 1
            for raw in tokenize(txt):  # nb_tokenizer -> list[str]
                tokens.append(
                    {
                        "book_id": book_id,
                        "seq": seq,
                        "raw": raw,
                        "cf": raw.casefold(),
                        "page": page,
                        "para": para,
                    }
                )
                seq += 1

    return tokens


if __name__ == "__main__":
    # Liten røyk-test: les noen få filer og skriv de første tokenene
    src = Path("20230119_bokselskap.no")
    for xml_path in sorted(src.glob("*.xml"))[:1]:
        toks = extract_tokens(xml_path)
        print(xml_path.name, "tokens:", len(toks))
        print(toks[:10])

