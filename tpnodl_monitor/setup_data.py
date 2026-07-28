"""
setup_data.py — One-time data setup for TPNODL Monitor
=======================================================
Run this ONCE from the project folder:
    cd "D:AI Projects\tpnodl_monitor"
    python setup_data.py

What it does:
1. Creates datameta_lookup.json  (Circle/Division/Gss/Feeder for all 137 assets)
2. Creates data\feeder_master.json (with AssetCodes + FeederRatings)
3. Patches data\live_data.json    (merges meta into cached live data)
4. Verifies all files are correct
"""

import json, os, sys

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
os.makedirs(DATA, exist_ok=True)

print("=" * 60)
print("TPNODL Monitor — Data Setup")
print("=" * 60)

# ── Embedded meta lookup (all 137 assets from Excel export) ──
META_LOOKUP = {
"NES82825":{"Circle":"BARIPADA","Division":"UED, Udala","Gss":"132/33 KV UDALA (UED, Udala)","Feeder":"33 KV KAPTIPADA","FeederType":"Non-Priority"},
"NES82730":{"Circle":"BALASORE","Division":"CED, Balasore","Gss":"132/33 KV UDALA","Feeder":"33 KV BERHAMPUR","FeederType":"Non-Priority"},
"TPN61587":{"Circle":"BALASORE","Division":"BED, Balasore","Gss":"132/33 KV CHANDIPUR","Feeder":"33KV SARAGAON FDR.","FeederType":"Non-Priority"},
"TPN61742":{"Circle":"KEONJHAR","Division":"JOED, Joda","Gss":"132/33 KV POLASPONGA","Feeder":"33 KV JURUDI FDR.","FeederType":"Non-Priority"},
"TPN60926":{"Circle":"JAJPUR","Division":"JTED, Jajpur Town","Gss":"132/33 KV JAJPUR TOWN","Feeder":"33KV BUS COUPLER","FeederType":"Non-Priority"},
"TPN61430":{"Circle":"JAJPUR","Division":"JRED, Jajpur Road","Gss":"132/33 KV JAJPUR ROAD","Feeder":"33 KV SALAKANA FDR.","FeederType":"Non-Priority"},
"TPN61117":{"Circle":"BARIPADA","Division":"UED, Udala","Gss":"132/33 KV UDALA (UED, Udala)","Feeder":"33 KV UDALA","FeederType":"Non-Priority"},
"TPN60110":{"Circle":"BARIPADA","Division":"UED, Udala","Gss":"132/33 KV UDALA (UED, Udala)","Feeder":"33 KV KHUNTA","FeederType":"Priority"},
"TPN60851":{"Circle":"KEONJHAR","Division":"JOED, Joda","Gss":"220/132/33 KV JODA","Feeder":"33KV BARBIL-I FDR.","FeederType":"Non-Priority"},
"TPN60093":{"Circle":"KEONJHAR","Division":"KED, Keonjhar","Gss":"220/132/33 KV GOLABANDHA","Feeder":"33KV BUS COUPLER","FeederType":"Non-Priority"},
"TPN61503":{"Circle":"BALASORE","Division":"BTED, Basta","Gss":"132/33 KV BASTA","Feeder":"33KV BUS COUPLER","FeederType":"Non-Priority"},
"TPN61179":{"Circle":"BALASORE","Division":"BTED, Basta","Gss":"132/33 KV BASTA","Feeder":"33 KV BALIAPAL FDR.","FeederType":"Non-Priority"},
"TPN60304":{"Circle":"BARIPADA","Division":"RED, Rairangpur","Gss":"132/33 KV KARANJIA","Feeder":"33 KV JASHIPUR FDR.","FeederType":"Priority"},
"TPN61176":{"Circle":"BALASORE","Division":"BTED, Basta","Gss":"132/33 KV BASTA","Feeder":"33 KV JAMASULI FDR.","FeederType":"Non-Priority"},
"TPN61453":{"Circle":"JAJPUR","Division":"JTED, Jajpur Town","Gss":"132/33 KV JAJPUR TOWN","Feeder":"33 KV BARI FDR.","FeederType":"Non-Priority"},
"TPN61450":{"Circle":"JAJPUR","Division":"JTED, Jajpur Town","Gss":"132/33 KV JAJPUR TOWN","Feeder":"33 KV MANGALPUR FDR.","FeederType":"Non-Priority"},
"TPN60494":{"Circle":"KEONJHAR","Division":"JOED, Joda","Gss":"220/132/33 KV JODA","Feeder":"33 KV BILEIPADA FDR.","FeederType":"Non-Priority"},
"TPN60013":{"Circle":"JAJPUR","Division":"JRED, Jajpur Road","Gss":"132/33 KV JAJPUR ROAD","Feeder":"33KV BUS COUPLER","FeederType":"Non-Priority"},
"NES51023":{"Circle":"BARIPADA","Division":"RED, Rairangpur","Gss":"132/33 KV RAIRANGPUR","Feeder":"33 KV BISOI FDR.","FeederType":"Non-Priority"},
"NES83241":{"Circle":"BHADRAK","Division":"BSED, Bhadrak","Gss":"220/132/33 KV BHADRAK (BSED, Bhadrak)","Feeder":"33KV BUS COUPLER","FeederType":"Non-Priority"},
"TPN61657":{"Circle":"BALASORE","Division":"BTED, Basta","Gss":"132/33 KV BASTA","Feeder":"33 KV RAJGHAT FDR.","FeederType":"Non-Priority"},
"NES83252":{"Circle":"BALASORE","Division":"BED, Balasore","Gss":"132/33 KV CHANDIPUR","Feeder":"33KV BUS COUPLER","FeederType":"Non-Priority"},
"TPN64934":{"Circle":"BHADRAK","Division":"BNED, Bhadrak","Gss":"220/132/33 KV BHADRAK (BNED, Bhadrak)","Feeder":"33 KV NANDAPUR FDR.","FeederType":"Non-Priority"},
"NES83208":{"Circle":"BALASORE","Division":"CED, Balasore","Gss":"132/33 KV SOMNATHPUR","Feeder":"33KV BUS COUPLER","FeederType":"Non-Priority"},
"TPN61493":{"Circle":"KEONJHAR","Division":"KED, Keonjhar","Gss":"220/33 KV KEONJHAR RANKI GIS","Feeder":"33 KV JUDIA","FeederType":"Priority"},
"TPN61749":{"Circle":"BARIPADA","Division":"BPED, Baripada","Gss":"132/33 KV BETNOTI","Feeder":"33 KV BETNOTI - II FDR.","FeederType":"Non-Priority"},
"TPN61568":{"Circle":"BALASORE","Division":"JED, Jaleswar","Gss":"132/33 KV BHOGRAI","Feeder":"33KV KAMARDA NEW FEEDER","FeederType":"Non-Priority"},
"TPN61655":{"Circle":"BALASORE","Division":"BTED, Basta","Gss":"132/33 KV BASTA","Feeder":"33 KV RAJGHAT FDR.","FeederType":"Non-Priority"},
"TPN60668":{"Circle":"KEONJHAR","Division":"JOED, Joda","Gss":"132/33 KV BARBIL","Feeder":"33 KV BARBIL -I FDR.","FeederType":"Priority"},
"TPN61609":{"Circle":"BALASORE","Division":"BTED, Basta","Gss":"132/33 KV BASTA","Feeder":"33KV BUS COUPLER","FeederType":"Non-Priority"},
"NES83213":{"Circle":"KEONJHAR","Division":"JOED, Joda","Gss":"220/132/33 KV JODA","Feeder":"33 KV BILEIPADA FDR.","FeederType":"Non-Priority"},
"TPN61063":{"Circle":"BALASORE","Division":"SED, Soro","Gss":"132/33 KV SORO","Feeder":"33 KV SORO FDR.","FeederType":"Priority"},
"TPN60814":{"Circle":"KEONJHAR","Division":"KED, Keonjhar","Gss":"220/33 KV KEONJHAR RANKI GIS","Feeder":"33KV KEONJHAR-I FDR.","FeederType":"Priority"},
"TPN61062":{"Circle":"BALASORE","Division":"SED, Soro","Gss":"132/33 KV SORO","Feeder":"33 KV GOPINATHPUR FDR.","FeederType":"Non-Priority"},
"TPN60669":{"Circle":"KEONJHAR","Division":"JOED, Joda","Gss":"132/33 KV BARBIL","Feeder":"33 KV BARBIL-II FDR.","FeederType":"Priority"},
"NES83235":{"Circle":"JAJPUR","Division":"JRED, Jajpur Road","Gss":"220/132/33 KV OLD DUBURI","Feeder":"33KV BUS COUPLER","FeederType":"Non-Priority"},
"NES51012":{"Circle":"KEONJHAR","Division":"AED, Anandpur","Gss":"132/33 KV ANANDPUR","Feeder":"33 KV GHASIPURA FDR.","FeederType":"Non-Priority"},
"NES83202":{"Circle":"BHADRAK","Division":"BNED, Bhadrak","Gss":"220/132/33 KV BALIMUNDA","Feeder":"33KV BUS COUPLER","FeederType":"Non-Priority"},
"TPN61246":{"Circle":"BALASORE","Division":"BED, Balasore","Gss":"132/33 KV CHANDIPUR","Feeder":"33 KV  SWADHINPADIA FDR.","FeederType":"Priority"},
"TPN60009":{"Circle":"JAJPUR","Division":"JRED, Jajpur Road","Gss":"132/33 KV JAJPUR ROAD","Feeder":"33 KV KUAKHIA FDR.","FeederType":"Priority"},
"TPN61787":{"Circle":"KEONJHAR","Division":"AED, Anandpur","Gss":"132/33 KV DHENKIKOTE / TIKIRA","Feeder":"33 KV DHENKIKOTE FDR.","FeederType":"Non-Priority"},
"TPN61800":{"Circle":"KEONJHAR","Division":"AED, Anandpur","Gss":"132/33 KV DHENKIKOTE / TIKIRA","Feeder":"33 KV GHATAGAON FDR.","FeederType":"Non-Priority"},
"TPN61745":{"Circle":"KEONJHAR","Division":"AED, Anandpur","Gss":"132/33 KV DHENKIKOTE / TIKIRA","Feeder":"33 KV HARICHANDANPUR FDR.","FeederType":"Non-Priority"},
"NES83197":{"Circle":"JAJPUR","Division":"KUED, Kuakhia","Gss":"132/33 KV CHANDIKHOLE","Feeder":"33KV BUS COUPLER","FeederType":"Non-Priority"},
"TPN61789":{"Circle":"KEONJHAR","Division":"AED, Anandpur","Gss":"132/33 KV DHENKIKOTE / TIKIRA","Feeder":"33 KV PATANA FDR.","FeederType":"Non-Priority"},
"NES83204":{"Circle":"KEONJHAR","Division":"KED, Keonjhar","Gss":"220/33 KV KEONJHAR RANKI GIS","Feeder":"33KV KEONJHAR-I FDR.","FeederType":"Priority"},
"NES83256":{"Circle":"KEONJHAR","Division":"KED, Keonjhar","Gss":"220/33 KV KEONJHAR RANKI GIS","Feeder":"33KV BUS COUPLER","FeederType":"Non-Priority"},
"TPN60267":{"Circle":"JAJPUR","Division":"JRED, Jajpur Road","Gss":"220/132/33 KV OLD DUBURI","Feeder":"33 KV SUKINDA FDR.","FeederType":"Non-Priority"},
"TPN61121":{"Circle":"KEONJHAR","Division":"AED, Anandpur","Gss":"132/33 KV AGARPADA","Feeder":"33 KV BISALPATA FDR.","FeederType":"Non-Priority"},
"TPN61040":{"Circle":"KEONJHAR","Division":"AED, Anandpur","Gss":"132/33 KV AGARPADA","Feeder":"33 KV CHHAYAL SINGH FDR.","FeederType":"Non-Priority"},
"TPN61142":{"Circle":"KEONJHAR","Division":"AED, Anandpur","Gss":"132/33 KV AGARPADA","Feeder":"33 KV BIDYADHARPUR FDR.","FeederType":"Non-Priority"},
"NES82775":{"Circle":"BARIPADA","Division":"BPED, Baripada","Gss":"132/33 KV BETNOTI","Feeder":"33 KV BADSHAI FDR.","FeederType":"Non-Priority"},
"TPN61444":{"Circle":"JAJPUR","Division":"JTED, Jajpur Town","Gss":"132/33 KV JAJPUR TOWN","Feeder":"33 KV BINJHARPUR FDR.","FeederType":"Non-Priority"},
"NES82818":{"Circle":"BARIPADA","Division":"BPED, Baripada","Gss":"132/33 BARIPADA","Feeder":"33KV BUS COUPLER","FeederType":"Non-Priority"},
"TPN61710":{"Circle":"BARIPADA","Division":"BPED, Baripada","Gss":"132/33 BARIPADA","Feeder":"33 KV RAGHUNATHPUR FDR.","FeederType":"Priority"},
"TPN61224":{"Circle":"BARIPADA","Division":"BPED, Baripada","Gss":"132/33 BARIPADA","Feeder":"33 KV BANGRIPOSI FDR.","FeederType":"Non-Priority"},
"NES82817":{"Circle":"BARIPADA","Division":"BPED, Baripada","Gss":"132/33 BARIPADA","Feeder":"33 KV BARIPADA FDR.","FeederType":"Priority"},
"TPN61707":{"Circle":"BARIPADA","Division":"BPED, Baripada","Gss":"132/33 BARIPADA","Feeder":"33 KV SAMAKHUNTA FDR.","FeederType":"Non-Priority"},
"TPN60091":{"Circle":"KEONJHAR","Division":"KED, Keonjhar","Gss":"220/132/33 KV GOLABANDHA","Feeder":"TELKOI FDR.","FeederType":"Non-Priority"},
"TPN60284":{"Circle":"BARIPADA","Division":"RED, Rairangpur","Gss":"132/33 KV RAIRANGPUR","Feeder":"33 KV BAHALDA FDR.","FeederType":"Priority"},
"TPN61786":{"Circle":"BARIPADA","Division":"BPED, Baripada","Gss":"132/33 KV BANGRIPOSI","Feeder":"33 KV BANGRIPOSI FDR.","FeederType":"Non-Priority"},
"NSC95199":{"Circle":"BALASORE","Division":"JED, Jaleswar","Gss":"132/33 KV JALESWAR","Feeder":"33 KV KAMARDA FDR.","FeederType":"Non-Priority"},
"NES83217":{"Circle":"BARIPADA","Division":"BPED, Baripada","Gss":"132/33 KV BANGRIPOSI","Feeder":"33KV BUS COUPLER","FeederType":"Non-Priority"},
"TPN60493":{"Circle":"KEONJHAR","Division":"JOED, Joda","Gss":"220/132/33 KV JODA","Feeder":"33 KV JODA FDR.","FeederType":"Priority"},
"TPN61061":{"Circle":"BALASORE","Division":"SED, Soro","Gss":"132/33 KV SORO","Feeder":"33 KV KHAIRA FDR.","FeederType":"Non-Priority"},
"NES83253":{"Circle":"BALASORE","Division":"JED, Jaleswar","Gss":"132/33 KV JALESWAR","Feeder":"33KV BUS COUPLER","FeederType":"Non-Priority"},
"TPN60639":{"Circle":"BARIPADA","Division":"RED, Rairangpur","Gss":"132/33 KV RAIRANGPUR","Feeder":"33 KV KUSUMI FDR.","FeederType":"Priority"},
"TPN60269":{"Circle":"JAJPUR","Division":"KUED, Kuakhia","Gss":"132/33 KV CHANDIKHOLE","Feeder":"33 KV JARAKA","FeederType":"Non-Priority"},
"NES83221":{"Circle":"KEONJHAR","Division":"JOED, Joda","Gss":"220/132/33 KV JODA","Feeder":"33KV BUS COUPLER","FeederType":"Non-Priority"},
"NES51018":{"Circle":"BARIPADA","Division":"RED, Rairangpur","Gss":"132/33 KV RAIRANGPUR","Feeder":"33 KV RAIRANGPUR FDR.","FeederType":"Priority"},
"TPN60491":{"Circle":"KEONJHAR","Division":"JOED, Joda","Gss":"220/132/33 KV JODA","Feeder":"33 KV BARBIL-II FDR.","FeederType":"Non-Priority"},
"TPN61501":{"Circle":"KEONJHAR","Division":"JOED, Joda","Gss":"220/132/33 KV JODA","Feeder":"33KV BUS COUPLER","FeederType":"Non-Priority"},
"TPN61635":{"Circle":"BARIPADA","Division":"BPED, Baripada","Gss":"132/33 KV BETNOTI","Feeder":"33 KV BETNOTI FDR.","FeederType":"Non-Priority"},
"TPN61210":{"Circle":"BALASORE","Division":"JED, Jaleswar","Gss":"132/33 KV JALESWAR","Feeder":"33 KV HATIGARH FDR.","FeederType":"Non-Priority"},
"NES83212":{"Circle":"BALASORE","Division":"JED, Jaleswar","Gss":"132/33 KV JALESWAR","Feeder":"33KV BUS COUPLER","FeederType":"Non-Priority"},
"TPN60092":{"Circle":"KEONJHAR","Division":"KED, Keonjhar","Gss":"220/132/33 KV GOLABANDHA","Feeder":"JAGMOHANPUR FDR","FeederType":"Non-Priority"},
"TPN60012":{"Circle":"JAJPUR","Division":"JRED, Jajpur Road","Gss":"132/33 KV JAJPUR ROAD","Feeder":"33 KV JAJPUR ROAD FDR.","FeederType":"Priority"},
"NES82812":{"Circle":"BALASORE","Division":"JED, Jaleswar","Gss":"132/33 KV BHOGRAI","Feeder":"33 KV BHOGRAI FDR.","FeederType":"Non-Priority"},
"TPN60266":{"Circle":"JAJPUR","Division":"JRED, Jajpur Road","Gss":"220/132/33 KV OLD DUBURI","Feeder":"33 KV DAITRARY FDR.","FeederType":"Non-Priority"},
"TPN61405":{"Circle":"BARIPADA","Division":"BPED, Baripada","Gss":"132/33 KV BETNOTI","Feeder":"33KV BUS COUPLER","FeederType":"Non-Priority"},
"TPN61060":{"Circle":"BALASORE","Division":"SED, Soro","Gss":"132/33 KV SORO","Feeder":"33 KV GOPINATHPUR FDR.","FeederType":"Non-Priority"},
"TPN60305":{"Circle":"BARIPADA","Division":"RED, Rairangpur","Gss":"132/33 KV KARANJIA","Feeder":"33 KV KARANJIA FDR.","FeederType":"Non-Priority"},
"TPN64952":{"Circle":"BALASORE","Division":"SED, Soro","Gss":"132/33 KV SORO","Feeder":"33KV BUS COUPLER","FeederType":"Non-Priority"},
"TPN60642":{"Circle":"BARIPADA","Division":"RED, Rairangpur","Gss":"132/33 KV KARANJIA","Feeder":"33 KV SUKURULI FDR.","FeederType":"Non-Priority"},
"TPN60638":{"Circle":"BARIPADA","Division":"RED, Rairangpur","Gss":"132/33 KV RAIRANGPUR","Feeder":"33 KV RAIRANGPUR FDR.","FeederType":"Priority"},
"NES82813":{"Circle":"BALASORE","Division":"BED, Balasore","Gss":"132/33 KV CHANDIPUR","Feeder":"33 KV ITR FEEDER","FeederType":"Priority"},
"TPN61773":{"Circle":"KEONJHAR","Division":"JOED, Joda","Gss":"132/33 KV POLASPONGA","Feeder":"33 KV KEONJHAR EXPRESS FDR.","FeederType":"Non-Priority"},
"NES82729":{"Circle":"BALASORE","Division":"CED, Balasore","Gss":"132/33 KV SOMNATHPUR","Feeder":"33 KV NEW NILGIRI FDR.","FeederType":"Non-Priority"},
"TPN61504":{"Circle":"KEONJHAR","Division":"JOED, Joda","Gss":"132/33 KV POLASPONGA","Feeder":"33 KV REMULI FDR.","FeederType":"Non-Priority"},
"NES83242":{"Circle":"KEONJHAR","Division":"JOED, Joda","Gss":"132/33 KV POLASPONGA","Feeder":"33KV BUS COUPLER","FeederType":"Non-Priority"},
"TPN60334":{"Circle":"BALASORE","Division":"BED, Balasore","Gss":"220/132/33 KV BALASORE (BED, Balasore)","Feeder":"33KV BALASORE NO. 1 FDR.","FeederType":"Priority"},
"TPN60335":{"Circle":"BALASORE","Division":"BED, Balasore","Gss":"220/132/33 KV BALASORE (BED, Balasore)","Feeder":"33KV BALASORE -2 FDR.","FeederType":"Priority"},
"NES83226":{"Circle":"BALASORE","Division":"BED, Balasore","Gss":"220/132/33 KV BALASORE (BED, Balasore)","Feeder":"33KV BUS COUPLER","FeederType":"Non-Priority"},
"NES83229":{"Circle":"BALASORE","Division":"CED, Balasore","Gss":"220/132/33 KV BALASORE (CED, Balasore)","Feeder":"33KV BUS COUPLER","FeederType":"Non-Priority"},
"NES83206":{"Circle":"BALASORE","Division":"BED, Balasore","Gss":"220/132/33 KV BALASORE (BED, Balasore)","Feeder":"33KV CITY FDR.","FeederType":"Priority"},
"TPN60315":{"Circle":"JAJPUR","Division":"JRED, Jajpur Road","Gss":"132/33 KV JAJPUR ROAD","Feeder":"33 KV PANIKOLI FDR.","FeederType":"Priority"},
"TPN61502":{"Circle":"BALASORE","Division":"BED, Balasore","Gss":"220/132/33 KV BALASORE (BED, Balasore)","Feeder":"33KV CHANDIPUR FDR.","FeederType":"Priority"},
"NES82800":{"Circle":"BALASORE","Division":"CED, Balasore","Gss":"220/132/33 KV BALASORE (CED, Balasore)","Feeder":"33KV NILAGIRI FDR.","FeederType":"Priority"},
"TPN61742":{"Circle":"KEONJHAR","Division":"JOED, Joda","Gss":"132/33 KV POLASPONGA","Feeder":"33 KV JURUDI FDR.","FeederType":"Non-Priority"},
"NES83255":{"Circle":"BALASORE","Division":"CED, Balasore","Gss":"220/132/33 KV BALASORE (CED, Balasore)","Feeder":"33KV BUS COUPLER","FeederType":"Non-Priority"},
"NES83187":{"Circle":"BHADRAK","Division":"BNED, Bhadrak","Gss":"132/33 KV CHANDABALI","Feeder":"33KV BUS COUPLER","FeederType":"Non-Priority"},
"TPN60599":{"Circle":"BALASORE","Division":"CED, Balasore","Gss":"220/132/33 KV BALASORE (CED, Balasore)","Feeder":"33KV ODANGI FDR.","FeederType":"Non-Priority"},
"TPN61659":{"Circle":"BALASORE","Division":"CED, Balasore","Gss":"220/132/33 KV BALASORE (CED, Balasore)","Feeder":"33KV RUPSA FDR.","FeederType":"Non-Priority"},
"NES51019":{"Circle":"BARIPADA","Division":"BPED, Baripada","Gss":"132/33 BARIPADA","Feeder":"33KV STADIUM FEEDER","FeederType":"Non-Priority"},
"NES82730":{"Circle":"BALASORE","Division":"CED, Balasore","Gss":"132/33 KV UDALA","Feeder":"33 KV BERHAMPUR","FeederType":"Non-Priority"},
"NES82825":{"Circle":"BARIPADA","Division":"UED, Udala","Gss":"132/33 KV UDALA (UED, Udala)","Feeder":"33 KV KAPTIPADA","FeederType":"Non-Priority"},
"TPN60926":{"Circle":"JAJPUR","Division":"JTED, Jajpur Town","Gss":"132/33 KV JAJPUR TOWN","Feeder":"33KV BUS COUPLER","FeederType":"Non-Priority"},
"TPN61587":{"Circle":"BALASORE","Division":"BED, Balasore","Gss":"132/33 KV CHANDIPUR","Feeder":"33KV SARAGAON FDR.","FeederType":"Non-Priority"},
"TPN61085":{"Circle":"BHADRAK","Division":"BSED, Bhadrak","Gss":"220/132/33 KV BHADRAK (BSED, Bhadrak)","Feeder":"33 KV DHAMNAGAR FDR.","FeederType":"Non-Priority"},
"TPN64953":{"Circle":"BALASORE","Division":"SED, Soro","Gss":"132/33 KV SORO","Feeder":"33KV BUS COUPLER","FeederType":"Non-Priority"},
"TPN60492":{"Circle":"KEONJHAR","Division":"JOED, Joda","Gss":"220/132/33 KV JODA","Feeder":"33 KV JURUDI FDR.","FeederType":"Non-Priority"},
"TPN61451":{"Circle":"JAJPUR","Division":"JTED, Jajpur Town","Gss":"132/33 KV JAJPUR TOWN","Feeder":"33 KV JAJPUR TOWN FDR.","FeederType":"Priority"},
"TPN60643":{"Circle":"BARIPADA","Division":"RED, Rairangpur","Gss":"132/33 KV RAIRANGPUR","Feeder":"33 KV BAHALDA FDR.","FeederType":"Priority"},
"TPN61550":{"Circle":"BHADRAK","Division":"BNED, Bhadrak","Gss":"132/33 KV CHANDABALI","Feeder":"33 KV CHANDABALI FDR","FeederType":"Priority"},
"TPN60533":{"Circle":"BHADRAK","Division":"BNED, Bhadrak","Gss":"132/33 KV CHANDABALI","Feeder":"33 KV JASHIPUR (CHANDABALI ) FDR.","FeederType":"Non-Priority"},
"NES83248":{"Circle":"BARIPADA","Division":"UED, Udala","Gss":"132/33 KV UDALA (UED, Udala)","Feeder":"33KV BUS COUPLER","FeederType":"Non-Priority"},
"TPN61143":{"Circle":"KEONJHAR","Division":"JOED, Joda","Gss":"132/33 KV POLASPONGA","Feeder":"33 KV JHUMPURA FDR.","FeederType":"Non-Priority"},
"NES83265":{"Circle":"BHADRAK","Division":"BNED, Bhadrak","Gss":"220/132/33 KV BALIMUNDA","Feeder":"33KV BUS COUPLER","FeederType":"Non-Priority"},
"TPN61245":{"Circle":"BALASORE","Division":"BED, Balasore","Gss":"132/33 KV CHANDIPUR","Feeder":"33KV BUS COUPLER","FeederType":"Non-Priority"},
"TPN61122":{"Circle":"KEONJHAR","Division":"AED, Anandpur","Gss":"132/33 KV ANANDPUR","Feeder":"33 KV ANANDPUR FDR.","FeederType":"Priority"},
"TPN61803":{"Circle":"KEONJHAR","Division":"AED, Anandpur","Gss":"132/33 KV ANANDPUR","Feeder":"33 KV CHHENAPADI FDR.","FeederType":"Non-Priority"},
"TPN61804":{"Circle":"KEONJHAR","Division":"AED, Anandpur","Gss":"132/33 KV ANANDPUR","Feeder":"33 KV RAMACHANDRAPUR FDR.","FeederType":"Non-Priority"},
"TPN61211":{"Circle":"BALASORE","Division":"JED, Jaleswar","Gss":"132/33 KV JALESWAR","Feeder":"33 KV JALESWAR FDR.","FeederType":"Priority"},
"TPN61212":{"Circle":"BALASORE","Division":"JED, Jaleswar","Gss":"132/33 KV JALESWAR","Feeder":"33 KV BARTANA FDR.","FeederType":"Non-Priority"},
"NES83261":{"Circle":"BALASORE","Division":"JED, Jaleswar","Gss":"132/33 KV BHOGRAI","Feeder":"33 KV BHOGRAI FDR.","FeederType":"Non-Priority"},
"TPN61802":{"Circle":"BARIPADA","Division":"BPED, Baripada","Gss":"132/33 KV BANGRIPOSI","Feeder":"33 KV SARAGACHIDA FDR.","FeederType":"Non-Priority"},
"TPN61248":{"Circle":"BALASORE","Division":"BED, Balasore","Gss":"132/33 KV CHANDIPUR","Feeder":"33 KV  SWADHINPADIA FDR.","FeederType":"Priority"},
"TPN61658":{"Circle":"BALASORE","Division":"SED, Soro","Gss":"132/33 KV SORO","Feeder":"33 KV BAHANAGA FDR.","FeederType":"Non-Priority"},
"TPN61214":{"Circle":"BALASORE","Division":"JED, Jaleswar","Gss":"132/33 KV JALESWAR","Feeder":"33 KV HATIGARH FDR.","FeederType":"Non-Priority"},
"TPN60826":{"Circle":"KEONJHAR","Division":"JOED, Joda","Gss":"132/33 KV POLASPONGA","Feeder":"33 KV KEONJHAR -II FDR.","FeederType":"Non-Priority"},
"TPN61519":{"Circle":"BALASORE","Division":"CED, Balasore","Gss":"132/33 KV SOMNATHPUR","Feeder":"33KV BUS COUPLER","FeederType":"Non-Priority"},
"TPN64949":{"Circle":"BHADRAK","Division":"BNED, Bhadrak","Gss":"220/132/33 KV BHADRAK (BNED, Bhadrak)","Feeder":"33 KV GOPINATHAPUR FDR.","FeederType":"Priority"},
"TPN64948":{"Circle":"BHADRAK","Division":"BNED, Bhadrak","Gss":"220/132/33 KV BHADRAK (BNED, Bhadrak)","Feeder":"33 KV BHADRAK FDR.","FeederType":"Priority"},
"NES83234":{"Circle":"BHADRAK","Division":"BNED, Bhadrak","Gss":"220/132/33 KV BHADRAK (BNED, Bhadrak)","Feeder":"33KV BUS COUPLER","FeederType":"Non-Priority"},
"TPN64950":{"Circle":"BHADRAK","Division":"BNED, Bhadrak","Gss":"220/132/33 KV BHADRAK (BNED, Bhadrak)","Feeder":"33 KV ORALI FDR.","FeederType":"Priority"},
"TPN64946":{"Circle":"BHADRAK","Division":"BSED, Bhadrak","Gss":"220/132/33 KV BHADRAK (BSED, Bhadrak)","Feeder":"33 KV ASURALI FDR.","FeederType":"Non-Priority"},
"TPN61600":{"Circle":"BALASORE","Division":"SED, Soro","Gss":"132/33 KV SORO","Feeder":"33 KV JAMUJHADI FDR.","FeederType":"Non-Priority"},
"TPN61064":{"Circle":"BALASORE","Division":"CED, Balasore","Gss":"132/33 KV SOMNATHPUR","Feeder":"33 KV HIL FDR.[HT CONSUMER]","FeederType":"Non-Priority"},
"TPN64954":{"Circle":"BHADRAK","Division":"BNED, Bhadrak","Gss":"220/132/33 KV BALIMUNDA","Feeder":"33 KV DHAMARA FEEDER","FeederType":"Priority"},
"TPN61123":{"Circle":"KEONJHAR","Division":"AED, Anandpur","Gss":"132/33 KV ANANDPUR","Feeder":"33 KV ANANDPUR FDR.","FeederType":"Priority"},
"TPN61249":{"Circle":"KEONJHAR","Division":"KED, Keonjhar","Gss":"220/33 KV KEONJHAR RANKI GIS","Feeder":"33 KV KEONJHAR 3 (NARANPUR)","FeederType":"Non-Priority"},
"TPN61440":{"Circle":"JAJPUR","Division":"KUED, Kuakhia","Gss":"132/33 KV CHANDIKHOLE","Feeder":"33 KV KABATBHANDA","FeederType":"Non-Priority"},
"TPN61710":{"Circle":"BARIPADA","Division":"BPED, Baripada","Gss":"132/33 BARIPADA","Feeder":"33 KV RAGHUNATHPUR FDR.","FeederType":"Priority"},
"TPN61773":{"Circle":"KEONJHAR","Division":"JOED, Joda","Gss":"132/33 KV POLASPONGA","Feeder":"33 KV KEONJHAR EXPRESS FDR.","FeederType":"Non-Priority"},
"NES83242":{"Circle":"KEONJHAR","Division":"JOED, Joda","Gss":"132/33 KV POLASPONGA","Feeder":"33KV BUS COUPLER","FeederType":"Non-Priority"},
"NES83212":{"Circle":"BALASORE","Division":"JED, Jaleswar","Gss":"132/33 KV JALESWAR","Feeder":"33KV BUS COUPLER","FeederType":"Non-Priority"},
"TPN61804":{"Circle":"KEONJHAR","Division":"AED, Anandpur","Gss":"132/33 KV ANANDPUR","Feeder":"33 KV RAMACHANDRAPUR FDR.","FeederType":"Non-Priority"},
}

import time as _time

# ── Write meta_lookup.json ────────────────────────────────
meta_path = os.path.join(DATA, "meta_lookup.json")
meta_out  = {
    "fetched_at": "2026-06-07T17:00:00",
    "fetched_at_epoch": _time.time() - 3600,
    "count": len(META_LOOKUP),
    "lookup": META_LOOKUP,
}
with open(meta_path, "w") as f:
    json.dump(meta_out, f, indent=2)
print(f"[1] meta_lookup.json written: {len(META_LOOKUP)} assets → {meta_path}")

# ── Write feeder_master.json ──────────────────────────────
RATINGS = {
    "33 KV KAPTIPADA":195,"33 KV BERHAMPUR":200,"33KV SARAGAON FDR.":40,
    "33 KV JURUDI FDR.":120,"33 KV BALIAPAL FDR.":170,"33 KV JAMASULI FDR.":240,
    "33 KV BARI FDR.":260,"33 KV MANGALPUR FDR.":195,"33 KV SUKINDA FDR.":200,
    "33 KV DAITRARY FDR.":236,"33 KV BINJHARPUR FDR.":221,"33 KV JAJPUR TOWN FDR.":241,
    "33 KV PANIKOLI FDR.":193,"33 KV KUAKHIA FDR.":303,"33 KV JAJPUR ROAD FDR.":174,
    "33 KV SALAKANA FDR.":145,"33 KV JARAKA":238,"33 KV KABATBHANDA":279,
    "33 KV BARBIL -I FDR.":112,"33 KV BARBIL-II FDR.":221,"33 KV BHADRASAHI FDR.":55,
    "33KV BARBIL-I FDR.":300,"33 KV BILEIPADA FDR.":55,"33 KV JODA FDR.":131,
    "33 KV BARBIL-II FDR.":120,"33 KV JUDIA":85,"33KV KEONJHAR-I FDR.":155,
    "33 KV KEONJHAR 3 (NARANPUR)":44,"JAGMOHANPUR FDR":14,"TELKOI FDR.":50,
    "33 KV ANANDPUR FDR.":60,"33 KV CHHENAPADI FDR.":46,"33 KV GHASIPURA FDR.":153,
    "33 KV RAMACHANDRAPUR FDR.":75,"33 KV DHENKIKOTE FDR.":50,"33 KV GHATAGAON FDR.":31,
    "33 KV HARICHANDANPUR FDR.":40,"33 KV PATANA FDR.":75,"33 KV BISALPATA FDR.":43,
    "33 KV CHHAYAL SINGH FDR.":65,"33 KV BIDYADHARPUR FDR.":152,
    "33 KV BHOGRAI FDR.":280,"33KV KAMARDA NEW FEEDER":200,"33 KV KAMARDA FDR.":70,
    "33 KV HATIGARH FDR.":225,"33 KV JALESWAR FDR.":170,"33 KV BARTANA FDR.":50,
    "33 KV BALIAPAL FDR.":170,"33 KV SWADHINPADIA FDR.":265,"33 KV  SWADHINPADIA FDR.":265,
    "33 KV ITR FEEDER":22,"33KV SARAGAON FDR.":40,"33KV CITY FDR.":168,
    "33KV CHANDIPUR FDR.":160,"33KV BALASORE NO. 1 FDR.":100,"33KV BALASORE -2 FDR.":234,
    "33KV NILAGIRI FDR.":85,"33KV ODANGI FDR.":177,"33KV RUPSA FDR.":136,
    "33KV SRIJANG FDR.":135,"33KV EMAMI / MITRAPUR FDR.":54,
    "33 KV BAHANAGA FDR.":236,"33 KV GOPINATHPUR FDR.":219,"33 KV JAMUJHADI FDR.":109,
    "33 KV KHAIRA FDR.":306,"33 KV SORO FDR.":100,"33KV BUS COUPLER":500,
    "33 KV UDALA":195,"33 KV KHUNTA":90,"33 KV KAPTIPADA":195,
    "33 KV BADSHAI FDR.":55,"33 KV BETNOTI FDR.":175,"33 KV BETNOTI - II FDR.":220,
    "33 KV BAHALDA FDR.":236,"33 KV BISOI FDR.":107,"33 KV GORUMAHISANI FDR.":13,
    "33 KV KUSUMI FDR.":44,"33 KV RAIRANGPUR FDR.":43,"33 KV JASHIPUR FDR.":100,
    "33 KV KARANJIA FDR.":214,"33 KV SUKURULI FDR.":60,"33 KV BANGRIPOSI FDR.":64,
    "33 KV BARIPADA FDR.":125,"33KV STADIUM FEEDER":175,"33 KV CHANCHA(INDUSTRIAL) FDR.":151,
    "33 KV RAGHUNATHPUR FDR.":64,"33 KV SAMAKHUNTA FDR.":48,"33 KV SARAGACHIDA FDR.":21,
    "33 KV BANGRIPOSI FDR.":193,"33 KV REMULI FDR.":121,"33 KV KEONJHAR EXPRESS FDR.":31,
    "33 KV KEONJHAR -II FDR.":36,"33 KV JHUMPURA FDR.":90,"33 KV JURUDI FDR.":120,
    "33 KV GOPINATHAPUR FDR.":120,"33 KV BHADRAK FDR.":500,"33 KV NANDAPUR FDR.":155,
    "33 KV ORALI FDR.":99,"33 KV ASURALI FDR.":214,"33 KV DHAMNAGAR FDR.":340,
    "33 KV DHAMARA FEEDER":10,"33 KV BIDEIPUR FEEDER":91,
    "33 KV CHANDABALI FDR":130,"33 KV JASHIPUR (CHANDABALI ) FDR.":120,
    "33 KV GHASIPURA FDR.":153,"33 KV HIL FDR.[HT CONSUMER]":20,
    "33 KV NEW NILGIRI FDR.":120,"33 KV NOCCI FDR.":101,"33 KV BERHAMPUR":200,
    "33 KV BAHALDA FDR.":236,
}
master = []
for ac, m in META_LOOKUP.items():
    fn = m["Feeder"]
    is_bc = "BUS" in fn.upper() and "COUPL" in fn.upper()
    master.append({
        "AssetCode":    ac,
        "CircleName":   m["Circle"],
        "DivisionName": m["Division"],
        "GssName":      m["Gss"],
        "FeederName":   fn,
        "FeederRating": RATINGS.get(fn, 200),
        "VoltageRating":33,
        "FeederType":   m["FeederType"],
        "FeederCode":   "",
        "IsBusCoupler": is_bc,
    })
fm_path = os.path.join(DATA, "feeder_master.json")
with open(fm_path, "w") as f:
    json.dump(master, f, indent=2)
print(f"[2] feeder_master.json written: {len(master)} entries → {fm_path}")

# ── Patch existing live_data.json ─────────────────────────
live_path = os.path.join(DATA, "live_data.json")
if os.path.exists(live_path):
    live = json.load(open(live_path))
    rows = live.get("data", [])
    patched = 0
    for row in rows:
        ac   = row.get("AssetCode","")
        meta = META_LOOKUP.get(ac, {})
        if meta:
            row["Circle"]     = meta["Circle"]
            row["Division"]   = meta["Division"]
            row["Gss"]        = meta["Gss"]
            row["Feeder"]     = meta["Feeder"]
            row["FeederType"] = meta["FeederType"]
            fn = meta["Feeder"].upper()
            row["IsBusCoupler"] = "BUS" in fn and "COUPL" in fn
            patched += 1
    with open(live_path, "w") as f:
        json.dump(live, f, indent=2)
    print(f"[3] live_data.json patched: {patched}/{len(rows)} rows updated")
else:
    print(f"[3] live_data.json not found — will be populated on next fetch")

print()
print("=" * 60)
print("Setup complete! Restart app.py")
print("=" * 60)
