"""Fantasy name generator — call generate(race, female) from other modules."""
import random

def _pick(lst): return random.choice(lst)
def _join(*parts): return "".join(parts).capitalize()


def halfling(female=False):
    if female:
        onset = _pick(["B","C","D","F","G","H","M","N","P","R","S","T","W",""])
        vowel = _pick(["a","e","i","o","ab","ib","el","il","al"])
        end   = _pick(["ble","lla","sse","la","ra","na","",""])
        mid   = _pick(["","r","s","l","n",""])
        if random.random() < 0.5:
            return _join(onset + vowel + mid + end)
        c2 = _pick(["B","C","D","F","G","H","M","N","P","R","S","T","W"])
        v2 = _pick(["a","e","i","y"])
        return _join(onset + vowel + c2 + v2)
    else:
        onset = _pick(["B","C","D","F","G","H","M","N","P","R","S","T","W",""])
        vowel = _pick(["a","e","i","o","u","ip","ob","am","il","im"])
        end   = _pick(["b","d","g","k","l","m","n","p","r","s","t","","",""])
        if random.random() < 0.6:
            return _join(onset + vowel + end)
        c2 = _pick(["b","d","g","k","l","m","n","p","r",""])
        v2 = _pick(["a","e","i","o"])
        return _join(onset + vowel + c2 + v2)


HUMAN_SURNAMES = [
    "Aldric","Barrow","Crestwell","Dane","Emsworth","Farrow","Greymantle",
    "Holt","Ironsides","Jance","Kemper","Lorne","Marsh","Nighthollow",
    "Oakhurst","Pendry","Quell","Rowe","Stonefield","Tanner","Ulvane",
    "Voss","Whitmore","Yorke","Ashford","Blackwood","Coldwater","Dunmore",
    "Eastwick","Fenwick","Godfrey","Hartwell","Ingram","Jasper",
]

def human(female=False):
    if female:
        onset = _pick(["Al","El","Br","C","D","Em","F","G","H","Is","J","K","L","M","N","Or","P","R","S","T","V","W",""])
        vowel = _pick(["a","e","i","o","u","ara","ella","enna","ina","ora","ira","ana","eva"])
        end   = _pick(["","","","n","na","ra","la","sse","nne","lyn","dra","via","cia"])
        first = _join(onset + vowel + end)
    else:
        onset = _pick(["Al","B","Br","C","D","E","F","G","H","J","K","L","M","N","Or","P","R","S","T","V","W",""])
        vowel = _pick(["a","e","i","o","u","ar","en","on","an","or","ur"])
        end   = _pick(["","","d","k","l","m","n","nd","r","rd","s","t","th","ck","rt"])
        first = _join(onset + vowel + end)
    return f"{first} {_pick(HUMAN_SURNAMES)}"


ELF_SURNAMES = [
    "Aelasar","Aeravansel","Amakiir","Amastacia","Galanodel","Holimion",
    "Ilphelkiir","Liadon","Meliamne","Naïlo","Siannodel","Xiloscient",
    "Moonwhisper","Starweave","Dawnmantle","Evenwood","Silverleaf",
]

def elf(female=False):
    if female:
        onset = _pick(["Ae","Al","An","Ar","Ath","C","E","El","F","G","I","L","M","N","R","S","T","V",""])
        mid   = _pick(["ara","eia","ella","ira","iel","iele","ina","ora","oria","ena","ela","eva","ala"])
        end   = _pick(["","","l","n","s","ra","lis","nel","ria","via","na","sse"])
        first = _join(onset + mid + end)
    else:
        onset = _pick(["Ae","Al","An","Ar","Ath","C","E","El","F","G","I","L","M","N","R","S","T","V",""])
        mid   = _pick(["ael","alar","amir","anor","ar","avel","el","elar","eron","ian","iel","ior","ir","or"])
        end   = _pick(["","","as","el","en","iel","il","ion","is","on","or","us"])
        first = _join(onset + mid + end)
    return f"{first} {_pick(ELF_SURNAMES)}"


DWARF_CLANS = [
    "Battlehammer","Boulderrock","Bronzemantle","Cragfist","Deepdelve",
    "Fireforge","Goldvein","Hammerfall","Ironbrow","Marblebrow",
    "Stoneshield","Thunderheim","Torunn","Undurr","Warcrown",
]

def dwarf(female=False):
    onset = _pick(["B","Br","D","Dr","G","Gr","H","K","M","N","T","Th","V",""])
    vowel = _pick(["a","e","i","o","u","ar","or","un","am","ur"])
    if female:
        end = _pick(["a","da","ga","ka","na","ra","ta","va","",""])
    else:
        end = _pick(["","d","g","k","l","m","n","nd","r","rd","k","t","th"])
    return f"{_join(onset + vowel + end)} {_pick(DWARF_CLANS)}"


GNOME_SURNAMES = [
    "Bafflestone","Cogsworth","Dundersnap","Fizzlewhistle","Glitterstone",
    "Hoppenbeck","Kettlewick","Nimblewhistle","Puddlejump","Quickfingers",
    "Sparklecap","Thistlewick","Wonderspark","Zigwhistle",
]

def gnome(female=False):
    onset = _pick(["B","C","D","F","G","H","J","K","L","M","N","P","R","S","T","W","Z",""])
    if female:
        vowel = _pick(["a","e","i","o","u","ia","ie","illy","ella","etta","ina"])
        end   = _pick(["","","","la","na","ra","za",""])
    else:
        vowel = _pick(["a","e","i","o","u","ick","ock","unk","ink","ank"])
        end   = _pick(["","","","n","k","t","l","r","s"])
    return f"{_join(onset + vowel + end)} {_pick(GNOME_SURNAMES)}"


def halfelf(female=False):
    if random.random() < 0.5:
        return elf(female).split()[0] + " " + _pick(HUMAN_SURNAMES)
    base   = human(female).split()[0]
    suffix = _pick(["","ael","el","an","ar","ien",""])
    return base + suffix + " " + _pick(ELF_SURNAMES)


def halforc(female=False):
    onset = _pick(["Ag","Br","D","Gr","Gur","H","K","M","N","R","Sh","T","Ug","Ul","Ur",""])
    vowel = _pick(["a","ag","ak","ar","ok","uk","um","ur","ush","usk"])
    if female:
        end = _pick(["a","ga","ka","ra","sha","",""])
    else:
        end = _pick(["","g","k","r","sh","t","th"])
    return _join(onset + vowel + end)


GENERATORS = {
    "halfling": halfling, "human": human,  "elf":     elf,
    "dwarf":    dwarf,    "gnome": gnome,  "halfelf": halfelf,
    "halforc":  halforc,
}
RACES = list(GENERATORS.keys())


def generate(race: str = "", female: bool = False) -> str:
    """Generate a name. race defaults to random if empty or unrecognised."""
    key = race.lower().replace("-", "").replace("_", "")
    fn  = GENERATORS.get(key) or GENERATORS[random.choice(RACES)]
    return fn(female=female)
