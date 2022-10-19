import collections, numpy as np, awkward as ak


from coffea import processor
from coffea.analysis_tools import Weights

from BTVNanoCommissioning.utils.correction import (
    load_lumi,
    eleSFs,
    muSFs,
    load_pu,
    load_BTV,
    load_jetfactory,
)
from BTVNanoCommissioning.helpers.func import flatten, update
from BTVNanoCommissioning.helpers.update_branch import missing_branch, add_jec
from BTVNanoCommissioning.helpers.cTagSFReader import getSF
from BTVNanoCommissioning.utils.AK4_parameters import correction_config
from BTVNanoCommissioning.utils.histogrammer import histogrammer
from BTVNanoCommissioning.utils.selection import (
    jet_id,
    mu_idiso,
    ele_mvatightid,
    softmu_mask,
)


class NanoProcessor(processor.ProcessorABC):
    def __init__(self, year="2017", campaign="Rereco17_94X", isCorr=True, isJERC=False):
        self._year = year
        self._campaign = campaign
        self.isCorr = isCorr
        self.isJERC = isJERC
        self.lumiMask = load_lumi(correction_config[self._campaign]["lumiMask"])
        ## Load corrections
        if isCorr:
            self._deepjetc_sf = load_BTV(
                self._campaign, correction_config[self._campaign]["BTV"], "DeepJetC"
            )
            self._deepjetb_sf = load_BTV(
                self._campaign, correction_config[self._campaign]["BTV"], "DeepJetB"
            )
            self._deepcsvc_sf = load_BTV(
                self._campaign, correction_config[self._campaign]["BTV"], "DeepCSVC"
            )
            self._deepcsvb_sf = load_BTV(
                self._campaign, correction_config[self._campaign]["BTV"], "DeepCSVB"
            )
            self._pu = load_pu(self._campaign, correction_config[self._campaign]["PU"])
        if isJERC:
            self._jet_factory = load_jetfactory(
                self._campaign, correction_config[self._campaign]["JME"]
            )
        ## Load histogram
        _hist_event_dict = histogrammer("ectag_Wc_sf")
        self.make_output = lambda: {
            "sumw": processor.defaultdict_accumulator(float),
            **_hist_event_dict,
        }

    @property
    def accumulator(self):
        return self._accumulator

    def process(self, events):
        output = self.make_output()
        dataset = events.metadata["dataset"]
        isRealData = not hasattr(events, "genWeight")
        events = missing_branch(events)
        weights = Weights(len(events), storeIndividual=True)
        if self.isJERC:
            add_jec(events, self._campaign, self._jet_factory)
        if isRealData:
            output["sumw"] = len(events)
        else:
            output["sumw"] = ak.sum(events.genWeight)
        ####################
        #    Selections    #
        ####################
        ## Lumimask
        req_lumi = np.ones(len(events), dtype="bool")
        if isRealData:
            req_lumi = self.lumiMask(events.run, events.luminosityBlock)

        ## HLT
        triggers = [
            "Ele32_WPTight_Gsf_L1DoubleEG",
        ]
        checkHLT = ak.Array([hasattr(events.HLT, _trig) for _trig in triggers])
        if ak.all(checkHLT == False):
            raise ValueError("HLT paths:", triggers, " are all invalid in", dataset)
        elif ak.any(checkHLT == False):
            print(np.array(triggers)[~checkHLT], " not exist in", dataset)
        trig_arrs = [
            events.HLT[_trig] for _trig in triggers if hasattr(events.HLT, _trig)
        ]
        req_trig = np.zeros(len(events), dtype="bool")
        for t in trig_arrs:
            req_trig = req_trig | t

        ## Electron cuts
        iso_ele = events.Electron[
            (events.Electron.pt > 34) & ele_mvatightid(events, self._campaign)
        ]
        req_ele = ak.count(iso_ele.pt, axis=1) == 1
        iso_ele = ak.pad_none(iso_ele, 1, axis=1)
        iso_ele = iso_ele[:, 0]

        ## Jet cuts
        event_jet = events.Jet[
            jet_id(events, self._campaign)
            & (ak.all(events.Jet.metric_table(iso_ele) > 0.5, axis=2))
        ]
        req_jets = (ak.num(event_jet.pt) >= 1) & (ak.num(event_jet.pt) <= 3)

        ## Soft Muon cuts
        soft_muon = events.Muon[softmu_mask(events, self._campaign)]
        req_softmu = ak.count(soft_muon.pt, axis=1) >= 1
        soft_muon = ak.pad_none(soft_muon, 1, axis=1)

        ## Muon-jet cuts
        mu_jet = event_jet[
            (ak.all(event_jet.metric_table(soft_muon) <= 0.4, axis=2))
            & ((event_jet.muonIdx1 != -1) | (event_jet.muonIdx2 != -1))
        ]
        req_mujet = ak.num(mu_jet.pt, axis=1) >= 1
        mu_jet = ak.pad_none(mu_jet, 1, axis=1)

        # Other cuts
        req_pTratio = (soft_muon[:, 0].pt / mu_jet[:, 0].pt) < 0.6

        req_QCDveto = (
            (iso_ele.pfRelIso03_all < 0.05)
            & (abs(iso_ele.dz) < 0.01)
            & (abs(iso_ele.dxy) < 0.002)
            & (iso_ele.ip3d < 0.2)
            & (
                (iso_ele.pt / mu_jet[:, 0].pt < 0.0)
                | (iso_ele.pt / mu_jet[:, 0].pt > 0.75)
            )
        )

        dilep_mu = events.Muon[(events.Muon.pt > 12) & mu_idiso(events, self._campaign)]
        dilep_ele = events.Electron[
            (events.Electron.pt > 15) & ele_mvatightid(events, self._campaign)
        ]
        req_dilepveto = (
            ak.count(dilep_mu.pt, axis=1) + ak.count(dilep_ele.pt, axis=1) != 2
        )

        MET = ak.zip(
            {
                "pt": events.MET.pt,
                "eta": ak.zeros_like(events.MET.pt),
                "phi": events.MET.phi,
                "mass": ak.zeros_like(events.MET.pt),
            },
            with_name="PtEtaPhiMLorentzVector",
        )
        Wmass = MET + iso_ele
        req_Wmass = Wmass.mass > 55

        event_level = (
            req_trig
            & req_lumi
            & req_ele
            & req_jets
            & req_softmu
            & req_mujet
            & req_Wmass
            & req_dilepveto
            & req_QCDveto
            & req_pTratio
        )
        event_level = ak.fill_none(event_level, False)

        ####################
        # Selected objects #
        ####################
        shmu = iso_ele[event_level]
        sjets = event_jet[event_level]
        ssmu = soft_muon[event_level]
        smet = ak.zip(
            {
                "pt": events[event_level].MET.pt,
                "eta": ak.zeros_like(events[event_level].MET.pt),
                "phi": events[event_level].MET.phi,
                "mass": ak.zeros_like(events[event_level].MET.pt),
            },
            with_name="PtEtaPhiMLorentzVector",
        )
        smuon_jet = mu_jet[event_level]
        smuon_jet = smuon_jet[:, 0]
        ssmu = ssmu[:, 0]
        sz = shmu + ssmu
        sw = shmu + smet
        osss = shmu.charge * ssmu.charge * -1
        njet = ak.count(sjets.pt, axis=1)

        ####################
        # Weight & Geninfo #
        ####################
        if not isRealData:
            weights.add("genweight", events.genWeight)
        if not isRealData and self.isCorr:
            weights.add(
                "puweight",
                self._pu[f"{self._year}_pileupweight"](events.Pileup.nPU),
            )
            weights.add(
                "lep1sf",
                np.where(
                    event_level,
                    eleSFs(
                        ak.firsts(
                            events.Electron[
                                (events.Electron.pt > 34)
                                & ele_mvatightid(events, self._campaign)
                            ]
                        ),
                        self._campaign,
                        correction_config[self._campaign]["LSF"],
                    ),
                    1.0,
                ),
            )
            weights.add(
                "lep2sf",
                np.where(
                    event_level,
                    muSFs(
                        ak.firsts(events.Muon[softmu_mask(events, self._campaign)]),
                        self._campaign,
                        correction_config[self._campaign]["LSF"],
                    ),
                    1.0,
                ),
            )

        if isRealData:
            genflavor = ak.zeros_like(sjets.pt)
            smflav = ak.zeros_like(smuon_jet.pt)
        else:
            genflavor = sjets.hadronFlavour + 1 * (
                (sjets.partonFlavour == 0) & (sjets.hadronFlavour == 0)
            )
            smflav = smuon_jet.hadronFlavour + 1 * (
                (smuon_jet.partonFlavour == 0) & (smuon_jet.hadronFlavour == 0)
            )
            jetsfs_c = collections.defaultdict(dict)
            jetsfs_b = collections.defaultdict(dict)
            csvsfs_c = collections.defaultdict(dict)
            csvsfs_b = collections.defaultdict(dict)
            if self.isCorr:
                jetsfs_c[0]["SF"] = getSF(
                    smuon_jet.hadronFlavour,
                    smuon_jet.btagDeepFlavCvL,
                    smuon_jet.btagDeepFlavCvB,
                    self._deepjetc_sf,
                )
                jetsfs_c[0]["SFup"] = getSF(
                    smuon_jet.hadronFlavour,
                    smuon_jet.btagDeepFlavCvL,
                    smuon_jet.btagDeepFlavCvB,
                    self._deepjetc_sf,
                    "TotalUncUp",
                )
                jetsfs_c[0]["SFdn"] = getSF(
                    smuon_jet.hadronFlavour,
                    smuon_jet.btagDeepFlavCvL,
                    smuon_jet.btagDeepFlavCvB,
                    self._deepjetc_sf,
                    "TotalUncDown",
                )
                jetsfs_b[0]["SF"] = self._deepjetb_sf.eval(
                    "central",
                    smuon_jet.hadronFlavour,
                    abs(smuon_jet.eta),
                    smuon_jet.pt,
                    discr=smuon_jet.btagDeepFlavB,
                )
                jetsfs_b[0]["SFup"] = self._deepjetb_sf.eval(
                    "up_jes",
                    smuon_jet.hadronFlavour,
                    abs(smuon_jet.eta),
                    smuon_jet.pt,
                    discr=smuon_jet.btagDeepFlavB,
                )
                jetsfs_b[0]["SFdn"] = self._deepjetb_sf.eval(
                    "down_jes",
                    smuon_jet.hadronFlavour,
                    abs(smuon_jet.eta),
                    smuon_jet.pt,
                    discr=smuon_jet.btagDeepFlavB,
                )
                csvsfs_c[0]["SF"] = getSF(
                    smuon_jet.hadronFlavour,
                    smuon_jet.btagDeepCvL,
                    smuon_jet.btagDeepCvB,
                    self._deepcsvc_sf,
                )
                csvsfs_c[0]["SFup"] = getSF(
                    smuon_jet.hadronFlavour,
                    smuon_jet.btagDeepCvL,
                    smuon_jet.btagDeepCvB,
                    self._deepcsvc_sf,
                    "TotalUncUp",
                )
                csvsfs_c[0]["SFdn"] = getSF(
                    smuon_jet.hadronFlavour,
                    smuon_jet.btagDeepCvL,
                    smuon_jet.btagDeepCvB,
                    self._deepcsvc_sf,
                    "TotalUncDown",
                )
                csvsfs_b[0]["SFup"] = self._deepcsvb_sf.eval(
                    "up_jes",
                    smuon_jet.hadronFlavour,
                    abs(smuon_jet.eta),
                    smuon_jet.pt,
                    discr=smuon_jet.btagDeepB,
                )
                csvsfs_b[0]["SF"] = self._deepcsvb_sf.eval(
                    "central",
                    smuon_jet.hadronFlavour,
                    abs(smuon_jet.eta),
                    smuon_jet.pt,
                    discr=smuon_jet.btagDeepB,
                )
                csvsfs_b[0]["SFdn"] = self._deepcsvb_sf.eval(
                    "down_jes",
                    smuon_jet.hadronFlavour,
                    abs(smuon_jet.eta),
                    smuon_jet.pt,
                    discr=smuon_jet.btagDeepB,
                )

                disc_list = {
                    "btagDeepB": csvsfs_b,
                    "btagDeepC": csvsfs_b,
                    "btagDeepFlavB": jetsfs_b,
                    "btagDeepFlavC": jetsfs_b,
                    "btagDeepCvL": csvsfs_c,
                    "btagDeepCvB": csvsfs_c,
                    "btagDeepFlavCvL": jetsfs_c,
                    "btagDeepFlavCvB": jetsfs_c,
                }

        ####################
        #  Fill histogram  #
        ####################
        for histname, h in output.items():
            if "Deep" in histname and "btag" not in histname:
                h.fill(
                    flatten(genflavor),
                    flatten(sjets[histname]),
                    weight=flatten(
                        ak.broadcast_arrays(
                            weights.weight()[event_level] * osss, sjets["pt"]
                        )[0]
                    ),
                )
            elif "jet_" in histname and "mu" not in histname:
                h.fill(
                    flatten(genflavor),
                    flatten(sjets[histname.replace("jet_", "")]),
                    weight=flatten(
                        ak.broadcast_arrays(
                            weights.weight()[event_level] * osss, sjets["pt"]
                        )[0]
                    ),
                )
            elif "hl_" in histname and histname.replace("hl_", "") in shmu.fields:
                h.fill(
                    flatten(shmu[histname.replace("hl_", "")]),
                    weight=weights.weight()[event_level] * osss,
                )
            elif (
                "soft_l" in histname and histname.replace("soft_l_", "") in ssmu.fields
            ):
                h.fill(
                    flatten(ssmu[histname.replace("soft_l_", "")]),
                    weight=weights.weight()[event_level] * osss,
                )
            elif "MET" in histname:
                h.fill(
                    flatten(events[event_level].MET[histname.replace("MET_", "")]),
                    weight=weights.weight()[event_level] * osss,
                )
            elif "mujet_" in histname:
                h.fill(
                    smflav,
                    flatten(smuon_jet[histname.replace("mujet_", "")]),
                    weight=weights.weight()[event_level] * osss,
                )
            elif "btagDeep" in histname and "0" in histname:
                h.fill(
                    flav=smflav,
                    syst="noSF",
                    discr=np.where(
                        smuon_jet[histname.replace("_0", "")] < 0,
                        -0.2,
                        smuon_jet[histname.replace("_0", "")],
                    ),
                    weight=weights.weight()[event_level] * osss,
                )
                if not isRealData and self.isCorr:
                    for syst in disc_list[histname.replace("_0", "")][0].keys():
                        h.fill(
                            flav=smflav,
                            syst=syst,
                            discr=np.where(
                                smuon_jet[histname.replace("_0", "")] < 0,
                                -0.2,
                                smuon_jet[histname.replace("_0", "")],
                            ),
                            weight=weights.weight()[event_level]
                            * disc_list[histname.replace("_0", "")][0][syst]
                            * osss,
                        )
            elif (
                "btagDeep" in histname and "1" in histname and all(i > 1 for i in njet)
            ):
                sljets = sjets[:, 1]
                h.fill(
                    flav=genflavor[:, 1],
                    syst="noSF",
                    discr=np.where(
                        sljets[histname.replace("_1", "")] < 0,
                        -0.2,
                        sljets[histname.replace("_1", "")],
                    ),
                    weight=weights.weight()[event_level] * osss,
                )
                if not isRealData and self.isCorr:
                    for syst in disc_list[histname.replace("_1", "")][1].keys():
                        h.fill(
                            flav=genflavor[:, 1],
                            syst=syst,
                            discr=np.where(
                                sljets[histname.replace("_1", "")] < 0,
                                -0.2,
                                sljets[histname.replace("_1", "")],
                            ),
                            weight=weights.weight()[event_level]
                            * disc_list[histname.replace("_1", "")][1][syst]
                            * osss,
                        )
        output["njet"].fill(njet, weight=weights.weight()[event_level] * osss)
        output["hl_ptratio"].fill(
            flav=genflavor[:, 0],
            ratio=shmu.pt / sjets[:, 0].pt,
            weight=weights.weight()[event_level] * osss,
        )
        output["soft_l_ptratio"].fill(
            flav=smflav,
            ratio=ssmu.pt / smuon_jet.pt,
            weight=weights.weight()[event_level] * osss,
        )
        output["dr_lmujetsmu"].fill(
            flav=smflav,
            dr=smuon_jet.delta_r(ssmu),
            weight=weights.weight()[event_level] * osss,
        )
        output["dr_lmujethmu"].fill(
            flav=smflav,
            dr=smuon_jet.delta_r(shmu),
            weight=weights.weight()[event_level] * osss,
        )
        output["dr_lmusmu"].fill(
            dr=shmu.delta_r(ssmu),
            weight=weights.weight()[event_level] * osss,
        )
        output["z_pt"].fill(flatten(sz.pt), weight=weights.weight()[event_level] * osss)
        output["z_eta"].fill(
            flatten(sz.eta), weight=weights.weight()[event_level] * osss
        )
        output["z_phi"].fill(
            flatten(sz.phi), weight=weights.weight()[event_level] * osss
        )
        output["z_mass"].fill(
            flatten(sz.mass), weight=weights.weight()[event_level] * osss
        )
        output["w_pt"].fill(flatten(sw.pt), weight=weights.weight()[event_level] * osss)
        output["w_eta"].fill(
            flatten(sw.eta), weight=weights.weight()[event_level] * osss
        )
        output["w_phi"].fill(
            flatten(sw.phi), weight=weights.weight()[event_level] * osss
        )
        output["w_mass"].fill(
            flatten(sw.mass), weight=weights.weight()[event_level] * osss
        )

        return {dataset: output}

    def postprocess(self, accumulator):
        return accumulator