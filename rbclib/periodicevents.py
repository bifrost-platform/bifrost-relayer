import logging
from typing import Optional, TYPE_CHECKING
import eth_abi

from chainpy.eth.ethtype.hexbytes import EthHashBytes
from chainpy.logger import formatted_log
from chainpy.eventbridge.chaineventabc import CallParamTuple, SendParamTuple
from chainpy.eventbridge.periodiceventabc import PeriodicEventABC
from chainpy.eventbridge.utils import timestamp_msec
from chainpy.btc.managers.simplerpccli import SimpleBtcClient
from chainpy.eth.ethtype.consts import ChainIndex
from chainpy.logger import Logger
from chainpy.offchain.priceaggregator import PriceOracleAgg

from relayer.metric import PrometheusExporterRelayer
from .chainevents import NoneParams, SOCKET_CONTRACT_NAME
from .relayersubmit import SocketSignature
from .consts import ConsensusOracleId, AggOracleId
from .utils import log_invalid_flow

if TYPE_CHECKING:
    from relayer.relayer import Relayer


price_logger = Logger("PriceUp", logging.INFO)
validator_logger = Logger("AuthDown", logging.INFO)
btc_logger = Logger("BTChash", logging.INFO)

CONSENSUS_ORACLE_FEEDING_FUNCTION_NAME = "oracle_consensus_feeding"
ROUND_UP_FUNCTION_NAME = "round_control_poll"


class PriceUpOracle(PeriodicEventABC):
    COLLECTION_PERIOD_SEC = 0  # means none
    COIN_IDS = []  # means none
    URL_DICT = dict()
    SOURCE_NAMES = []

    def __init__(self,
                 relayer: "Relayer",
                 period_sec: int = 0,
                 time_lock: int = timestamp_msec(),
                 price_cli: PriceOracleAgg = None
                 ):
        if self.__class__.COLLECTION_PERIOD_SEC == 0 or not self.__class__.COIN_IDS:
            raise Exception("call \"PriceUpOracle.setup() first\"")

        if period_sec == 0:
            period_sec = self.__class__.COLLECTION_PERIOD_SEC

        super().__init__(relayer, period_sec, time_lock)

        if price_cli is not None:
            self.__cli = price_cli
        else:
            self.__cli = PriceOracleAgg(
                self.__class__.SOURCE_NAMES,
                self.__class__.URL_DICT
            )
        PrometheusExporterRelayer.exporting_running_time_metric()

    @staticmethod
    def setup(coin_names: list, source_names: list, url_dict: dict, collection_period_sec: int):
        PriceUpOracle.COIN_IDS = coin_names
        PriceUpOracle.COLLECTION_PERIOD_SEC = collection_period_sec
        PriceUpOracle.URL_DICT = url_dict
        PriceUpOracle.SOURCE_NAMES = source_names

    @property
    def relayer(self) -> "Relayer":
        return self.manager

    def clone_next(self):
        return self.__class__(self.relayer, self.period_sec, self.time_lock + self.period_sec * 1000, self.__cli)

    def summary(self) -> str:
        return "{}".format(self.__class__.__name__)

    def build_call_transaction_params(self) -> CallParamTuple:
        log_invalid_flow(price_logger, self)
        return ChainIndex.NONE, "", "", []

    def build_transaction_params(self) -> SendParamTuple:
        # check whether this is current authority
        auth = self.relayer.is_validator(ChainIndex.BIFROST, self.relayer.active_account.address)
        if not auth:
            return NoneParams

        # dictionary of prices (key: coin id)
        collected_prices = self.__cli.get_current_weighted_price(self.__class__.COIN_IDS)

        # build oid list and prices list
        oid_list = [eval("AggOracleId.{}_PRICE".format(coin_id)) for coin_id in self.__class__.COIN_IDS]
        oid_list = [oid.formatted_bytes() for oid in oid_list]
        prices = [value for value in collected_prices.values()]
        PrometheusExporterRelayer.exporting_asset_prices(self.__class__.COIN_IDS, prices)

        formatted_log(
            price_logger,
            relayer_addr=self.relayer.active_account.address,
            log_id=self.__class__.__name__,
            related_chain=ChainIndex.BIFROST,
            # log_data=str("/".join([price.float_str for price in prices]))
            log_data="price-feeding"
        )
        return ChainIndex.BIFROST, "socket", "oracle_aggregate_feeding", [
            oid_list,
            [price.bytes() for price in prices]
        ]

    def handle_call_result(self, result: tuple) -> Optional[PeriodicEventABC]:
        log_invalid_flow(price_logger, self)
        return None

    def handle_tx_result_success(self) -> Optional[PeriodicEventABC]:
        return None

    def handle_tx_result_fail(self) -> Optional[PeriodicEventABC]:
        log_invalid_flow(price_logger, self)
        return None

    def handle_tx_result_no_receipt(self) -> Optional[PeriodicEventABC]:
        log_invalid_flow(price_logger, self)
        return None


class BtcHashUpOracle(PeriodicEventABC):
    URL = ""
    AUTH_ID = ""
    AUTH_PASSWD = ""
    COLLECTION_PERIOD_SEC = 0

    def __init__(self,
                 relayer: "Relayer",
                 period_sec: int = 0,
                 time_lock: int = timestamp_msec(),
                 btc_cli: SimpleBtcClient = None
                 ):

        if not self.__class__.URL or self.__class__.COLLECTION_PERIOD_SEC == 0:
            raise Exception("call \"BtcHashUpOracle.setup() first\"")

        if period_sec == 0:
            period_sec = self.__class__.COLLECTION_PERIOD_SEC

        super().__init__(relayer, period_sec, time_lock)

        if btc_cli is None:
            btc_cli = SimpleBtcClient(self.__class__.URL, 1, self.__class__.AUTH_ID, self.__class__.AUTH_PASSWD)
        self.__cli = btc_cli

        self.delayed = False
        PrometheusExporterRelayer.exporting_running_time_metric()

    @staticmethod
    def setup(url: str, auth_id: str = None, auth_passwd: str = None, collect_period_sec: int = 60):
        BtcHashUpOracle.URL = url
        BtcHashUpOracle.AUTH_ID = auth_id
        BtcHashUpOracle.AUTH_PASSWD = auth_passwd
        BtcHashUpOracle.COLLECTION_PERIOD_SEC = collect_period_sec

    @property
    def relayer(self) -> "Relayer":
        return self.manager

    def clone_next(self):
        period_msec = self.period_sec * 1000
        time_lock = self.time_lock + period_msec // 10 if self.delayed else self.time_lock + period_msec
        return self.__class__(self.relayer, self.period_sec, time_lock, self.__cli)

    def summary(self) -> str:
        return "{}".format(self.__class__.__name__)

    def build_call_transaction_params(self) -> CallParamTuple:
        log_invalid_flow(btc_logger, self)
        return ChainIndex.NONE, "", "", []

    def build_transaction_params(self) -> SendParamTuple:
        # check whether this is current authority
        auth = self.relayer.is_validator(ChainIndex.BIFROST, self.relayer.active_account.address)
        if not auth:
            return NoneParams

        latest_height_from_socket = self.relayer.fetch_oracle_latest_round(ConsensusOracleId.BTC_HASH)
        latest_height_from_chain = self.__cli.get_latest_confirmed_height()

        delta = latest_height_from_chain - latest_height_from_socket
        if delta == 0:
            return NoneParams

        if delta < 0:
            # critical error
            formatted_log(
                btc_logger,
                relayer_addr=self.relayer.active_account.address,
                log_id=self.__class__.__name__,
                related_chain=ChainIndex.BIFROST,
                log_data="oracle-error:OracleHeight({})>BtcHeight({})".format(
                    latest_height_from_socket, latest_height_from_chain
                )
            )
            return NoneParams

        self.delayed = True if delta > 1 else False

        feed_target_height = latest_height_from_socket + 1
        submitted = self.relayer.is_submitted_oracle_feed(ConsensusOracleId.BTC_HASH, feed_target_height)
        if not submitted:
            result = self.__cli.get_block_hash_by_height(feed_target_height)
            block_hash = EthHashBytes(result)
            PrometheusExporterRelayer.exporting_btc_hash(feed_target_height)
            formatted_log(
                btc_logger,
                relayer_addr=self.manager.active_account.address,
                log_id=self.__class__.__name__,
                related_chain=ChainIndex.BIFROST,
                log_data="btcHash({}):height({})".format(block_hash.hex(), feed_target_height)
            )

            return ChainIndex.BIFROST, SOCKET_CONTRACT_NAME, CONSENSUS_ORACLE_FEEDING_FUNCTION_NAME, [
                [ConsensusOracleId.BTC_HASH.formatted_bytes()],
                [feed_target_height],
                [block_hash.bytes()]
            ]

        else:
            formatted_log(
                btc_logger,
                relayer_addr=self.manager.active_account.address,
                log_id=self.__class__.__name__,
                related_chain=ChainIndex.BIFROST,
                log_data="submitted:height({})".format(feed_target_height)
            )
            return NoneParams

    def handle_call_result(self, result: tuple) -> Optional[PeriodicEventABC]:
        log_invalid_flow(price_logger, self)
        return None

    def handle_tx_result_success(self) -> Optional[PeriodicEventABC]:
        return None

    def handle_tx_result_fail(self) -> Optional[PeriodicEventABC]:
        log_invalid_flow(price_logger, self)
        return None

    def handle_tx_result_no_receipt(self) -> Optional[PeriodicEventABC]:
        log_invalid_flow(price_logger, self)
        return None

    def gas_limit_multiplier(self) -> float:
        return 2.0


class AuthDownOracle(PeriodicEventABC):
    COLLECTION_PERIOD_SEC = 0

    def __init__(self,
                 relayer: "Relayer",
                 period_sec: int = 0,
                 time_lock: int = timestamp_msec(),
                 _round: int = None):
        if self.__class__.COLLECTION_PERIOD_SEC == 0:
            raise Exception("call \"AuthDownOracle.setup() first\"")
        if period_sec == 0:
            period_sec = self.__class__.COLLECTION_PERIOD_SEC
        super().__init__(relayer, period_sec, time_lock)
        if _round is None:
            supported_chain_list = self.relayer.supported_chain_list
            supported_chain_list.remove(ChainIndex.BIFROST)
            self.__current_round = self.relayer.fetch_lowest_validator_round()
        else:
            self.__current_round = _round
        PrometheusExporterRelayer.exporting_running_time_metric()

    @staticmethod
    def setup(collect_period_sec: int = 60):
        AuthDownOracle.COLLECTION_PERIOD_SEC = collect_period_sec

    @property
    def relayer(self) -> "Relayer":
        return self.manager

    @property
    def current_round(self) -> int:
        return self.__current_round

    @current_round.setter
    def current_round(self, rnd: int):
        self.__current_round = rnd

    def clone_next(self):
        return self.__class__(
            self.relayer,
            self.period_sec,
            self.time_lock + self.period_sec * 1000,
            self.current_round
        )

    def summary(self) -> str:
        return "{}".format(self.__class__.__name__)

    def build_call_transaction_params(self) -> CallParamTuple:
        log_invalid_flow(price_logger, self)
        return ChainIndex.NONE, "", "", []

    def build_transaction_params(self) -> SendParamTuple:
        round_from_bn = self.relayer.fetch_validator_round(ChainIndex.BIFROST)

        for chain_index in self.relayer.supported_chain_list:
            rnd = self.relayer.fetch_validator_round(chain_index)
            PrometheusExporterRelayer.exporting_external_chain_rnd(chain_index, rnd)

        formatted_log(
            validator_logger,
            relayer_addr=self.relayer.active_account.address,
            log_id="CheckAuthority",
            related_chain=ChainIndex.BIFROST,
            log_data="cached({}):fetched({})".format(self.__current_round, round_from_bn)
        )

        if self.__current_round >= round_from_bn:
            return NoneParams

        if not self.relayer.is_previous_validator(
                ChainIndex.BIFROST, round_from_bn - 1, self.relayer.active_account.address
        ):
            self.current_round = round_from_bn
            return NoneParams

        sorted_validator_list = self.relayer.fetch_sorted_validator_list(ChainIndex.BIFROST)

        types_str_list = ["uint256", "address[]"]
        data_to_sig = eth_abi.encode_abi(types_str_list, [round_from_bn, sorted_validator_list])
        sig = self.relayer.active_account.ecdsa_recoverable_sign(data_to_sig)
        socket_sig = SocketSignature.from_single_sig(sig.r, sig.s, sig.v + 27)

        self.current_round = round_from_bn
        submit_data = [(round_from_bn, sorted_validator_list, socket_sig.tuple())]
        return ChainIndex.BIFROST, SOCKET_CONTRACT_NAME, ROUND_UP_FUNCTION_NAME, submit_data

    def handle_call_result(self, result: tuple) -> Optional[PeriodicEventABC]:
        log_invalid_flow(price_logger, self)
        return None

    def handle_tx_result_success(self) -> Optional[PeriodicEventABC]:
        return None

    def handle_tx_result_fail(self) -> Optional[PeriodicEventABC]:
        return None

    def handle_tx_result_no_receipt(self) -> Optional[PeriodicEventABC]:
        log_invalid_flow(price_logger, self)
        return None
