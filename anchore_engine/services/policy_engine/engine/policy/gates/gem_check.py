from anchore_engine.services.policy_engine.engine.policy.gate import Gate, BaseTrigger
from anchore_engine.services.policy_engine.engine.policy.utils import NameVersionListValidator, CommaDelimitedStringListValidator, barsplit_comma_delim_parser, delim_parser
from anchore_engine.db import GemMetadata
from anchore_engine.services.policy_engine.engine.logs import get_logger
from anchore_engine.services.policy_engine.engine.feeds import DataFeeds

log = get_logger()

# TODO; generalize these for any feed, with base classes and children per feed type

FEED_KEY = 'gem'
GEM_MATCH_KEY= 'matched_feed_gems'
GEM_LIST_KEY = 'gems'

class NotLatestTrigger(BaseTrigger):
    __trigger_name__ = 'GEMNOTLATEST'
    __description__ = 'triggers if an installed GEM is not the latest version according to GEM data feed'

    def evaluate(self, image_obj, context):
        """
        Fire for any gem in the image that is in the official gem feed but is not the latest version.
        Mutually exclusive to GEMNOTOFFICIAL and GEMBADVERSION

        """
        feed_gems = context.data.get(GEM_MATCH_KEY)
        img_gems = context.data.get(GEM_LIST_KEY)

        if feed_gems or not img_gems:
            return

        feed_names = {p.name: p.latest for p in feed_gems}

        for gem, versions in img_gems.items():
            if gem not in feed_names:
                continue # Not an official

            for v in versions:
                if v and v != feed_names.get(gem):
                    self._fire("GEMNOTLATEST Package ("+gem+") version ("+v+") installed but is not the latest version ("+feed_names[gem]['latest']+")")


class NotOfficialTrigger(BaseTrigger):
    __trigger_name__ = 'GEMNOTOFFICIAL'
    __description__ = 'triggers if an installed GEM is not in the official GEM database, according to GEM data feed'

    def evaluate(self, image_obj, context):
        """
        Fire for any gem that is not in the official gem feed data set.

        Mutually exclusive to GEMNOTLATEST and GEMBADVERSION

        :param image_obj:
        :param context:
        :return:
        """

        feed_gems = context.data.get(GEM_MATCH_KEY)
        img_gems = context.data.get(GEM_LIST_KEY)

        if feed_gems or not img_gems:
            return

        feed_names = {p.name: p.versions_json for p in feed_gems}

        for gem in img_gems.keys():
            if gem not in feed_names:
                self._fire(msg="GEMNOTOFFICIAL Package ("+str(gem)+") in container but not in official GEM feed.")


class BadVersionTrigger(BaseTrigger):
    __trigger_name__ = 'GEMBADVERSION'
    __description__ = 'triggers if an installed GEM version is not listed in the official GEM feed as a valid version'

    def evaluate(self, image_obj, context):
        """
        Fire for any gem that is in the official gem set but is not one of the official versions.

        Mutually exclusive to GEMNOTOFFICIAL and GEMNOTLATEST

        :param image_obj:
        :param context:
        :return:
        """
        feed_gems = context.data.get(GEM_MATCH_KEY)
        img_gems = context.data.get(GEM_LIST_KEY)

        if feed_gems or not img_gems:
            return

        feed_names = {p.name: p.versions_json for p in feed_gems}

        for gem, versions in img_gems.items():
            if gem not in feed_names:
                continue

            non_official_versions = set(versions).difference(set(feed_names.get(gem, [])))
            for v in non_official_versions:
                self._fire(msg="GEMBADVERSION Package ("+gem+") version ("+v+") installed but version is not in the official feed for this package ("+str(feed_names.get(gem, '')) + ")")


class PkgFullMatchTrigger(BaseTrigger):
    __trigger_name__ = 'GEMPKGFULLMATCH'
    __description__ = 'triggers if the evaluated image has an GEM package installed that matches one in the list given as a param (package_name|vers)'
    __params__ = {
        'BLACKLIST_GEMFULLMATCH': NameVersionListValidator()
    }

    def evaluate(self, image_obj, context):
        """
        Fire for any gem that is on the blacklist with a full name + version match
        :param image_obj:
        :param context:
        :return:
        """
        gems = image_obj.gems
        if not gems:
            return

        pkgs = context.data.get(GEM_LIST_KEY)
        if not pkgs:
            return

        for pkg, vers in barsplit_comma_delim_parser(self.eval_params.get('BLACKLIST_GEMFULLMATCH', '')).items():
            if pkg in pkgs and vers in pkgs.get(pkg, []):
                self._fire(msg='GEMPKGFULLMATCH Package is blacklisted: '+pkg+"-"+vers)


class PkgNameMatchTrigger(BaseTrigger):
    __trigger_name__ = 'GEMPKGNAMEMATCH'
    __description__ = 'triggers if the evaluated image has an GEM package installed that matches one in the list given as a param (package_name)'
    __params__ = {
        'BLACKLIST_GEMNAMEMATCH': CommaDelimitedStringListValidator()
    }

    def evaluate(self, image_obj, context):
        gems = image_obj.gems
        if not gems:
            return

        pkgs = context.data.get(GEM_LIST_KEY)
        if not pkgs:
            return

        for match_val in delim_parser(self.eval_params.get('BLACKLIST_GEMNAMEMATCH', '')):
            if match_val and match_val in pkgs:
                self._fire(msg='GEMPKGNAMEMATCH Package is blacklisted: ' + match_val)


class NoFeedTrigger(BaseTrigger):
    __trigger_name__ =  'GEMNOFEED'
    __description__ = 'triggers if anchore does not have access to the GEM data feed'
    __msg__ = "GEMNOFEED GEM packages are present but the anchore GEM feed is not available - will be unable to perform checks that require feed data"

    def evaluate(self, image_obj, context):
        try:
            feed_meta = DataFeeds.instance().packages.group_by_name(FEED_KEY)
            if feed_meta and feed_meta[0].last_sync:
                return
        except Exception as e:
            log.exception('Error determining feed presence for gems. Defaulting to firing trigger')

        self._fire()
        return


class GemCheckGate(Gate):
        __gate_name__ = "GEMCHECK"
        __triggers__ = [
            NotLatestTrigger,
            NotOfficialTrigger,
            BadVersionTrigger,
            PkgFullMatchTrigger,
            PkgNameMatchTrigger,
            NoFeedTrigger
        ]

        def prepare_context(self, image_obj, context):
            """
            Prep the gem names and versions
            :param image_obj:
            :param context:
            :return:
            """

            if not image_obj.gems:
                return context

            context.data[GEM_LIST_KEY] = {p.name: p.versions_json for p in image_obj.gems}
            context.data[GEM_MATCH_KEY] = []
            gems = context.data[GEM_LIST_KEY].keys()

            # Use a chunked fetch approach to avoid a single large in() statement with 1000+ keys
            chunks = [gems[i: i+100] for i in xrange(0, len(gems), 100)]
            for key_range in chunks:
                context.data[GEM_MATCH_KEY] += context.db.query(GemMetadata).filter(GemMetadata.name.in_(key_range)).all()


            return context
