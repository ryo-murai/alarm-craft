import logging
from time import sleep
from typing import Any, Iterable

import boto3

from .models import AlarmProps, MetricAlarmParam

logger = logging.getLogger(__name__)


class AlarmHandler:
    """Alarm Handler"""

    def __init__(self, config: dict[str, Any]) -> None:
        """Constructor

        Args:
            config (dict[str, Any]): config dict
        """
        self.cloudwatch = boto3.client("cloudwatch")
        self.config = config

    def __del__(self) -> None:
        """Desctructor"""
        # self.cloudwatch.close()
        pass

    def get_alarms_change_set(
        self, alarm_params: Iterable[MetricAlarmParam]
    ) -> tuple[Iterable[AlarmProps], Iterable[AlarmProps], list[str]]:
        """Gets alarm change set

        Args:
            alarm_params (Iterable[MetricAlarmParam]): alarms to create

        Returns:
            tuple[Iterable[AlarmProps], list[str], list[str]]: a tuple of alarms to create,
                                                                     alarms to keep and alarms to delete
        """
        alarm_name_prefix = self.config["globals"]["alarm"]["alarm_name_prefix"]
        required_alarms = list(self._get_required_alarm_params(alarm_name_prefix, alarm_params))
        current_alarms = self._get_current_alarms(alarm_name_prefix)
        current_alarm_names = {alm["AlarmName"] for alm in current_alarms}
        required_alarm_names = {alm["AlarmName"] for alm in required_alarms}

        need_to_create = [alm for alm in required_alarms if alm["AlarmName"] not in current_alarm_names]
        no_update = [alm for alm in required_alarms if alm["AlarmName"] in current_alarm_names]
        need_to_delete = current_alarm_names.difference(required_alarm_names)

        return (need_to_create, no_update, list(need_to_delete))

    def _get_required_alarm_params(
        self, alarm_name_prefix: str, alarm_params: Iterable[MetricAlarmParam]
    ) -> Iterable[AlarmProps]:
        for alm in alarm_params:
            resource_name = alm["TargetResource"]["ResourceName"]
            alarm_props = alm["AlarmProps"]
            alarm_props["AlarmName"] = f"{alarm_name_prefix}{resource_name}-{alarm_props['MetricName']}"
            alarm_props["AlarmDescription"] = f"Metric Alarm for `{alarm_props['MetricName']}` of {resource_name}"

            yield alarm_props

    def _get_current_alarms(self, alarm_name_prefix: str) -> Iterable:
        token = ""
        while True:
            current_alarms_resp = self.cloudwatch.describe_alarms(
                NextToken=token,
                AlarmNamePrefix=alarm_name_prefix,
                AlarmTypes=["MetricAlarm"],
                MaxRecords=100,
            )
            yield from current_alarms_resp["MetricAlarms"]

            next_token = current_alarms_resp.get("NextToken")
            if next_token and next_token != "":
                token = next_token
            else:
                # no more results
                break

    def update_alarms(
        self, to_create: Iterable[AlarmProps], to_delete: list[str], additional_alarm_actions: list[str]
    ) -> None:
        """Updates alarms with given changes

        Args:
            to_create (Iterable[AlarmProps]): alarms to create
            to_delete (list[str]): alarms to delete
            additional_alarm_actions (list[str]): alarm actions for created alarms
        """
        self._create_alarms(to_create, additional_alarm_actions)
        self._delete_alarms(to_delete)

    def _create_alarms(self, alarm_params: Iterable[AlarmProps], additional_alarm_actions: list[str]) -> None:
        common_param = self.config["globals"]["alarm"]["default_alarm_params"]

        # alarm actions
        alarm_notif_distinations = self.config["globals"]["alarm"]["alarm_actions"] + additional_alarm_actions
        common_param["AlarmActions"] = alarm_notif_distinations
        common_param["OKActions"] = alarm_notif_distinations
        common_param["InsufficientDataActions"] = alarm_notif_distinations

        # tagging
        alarm_tagging = self.config["globals"]["alarm"].get("alarm_tagging")
        if alarm_tagging:
            common_param["Tags"] = [{"Key": k, "Value": v} for k, v in alarm_tagging.items()]

        # interval for API calls
        interval = self._get_interval_in_sec()

        for alm in alarm_params:
            alarm_param = common_param | alm

            logger.debug("creating alarm:%s", alarm_param)
            resp = self.cloudwatch.put_metric_alarm(**alarm_param)
            respm = resp["ResponseMetadata"]
            logger.debug(
                f"status={respm['HTTPStatusCode']}, {respm['HTTPHeaders']}, {respm['RetryAttempts']} retries made."
            )
            if interval > 0:
                sleep(interval)

    def _delete_alarms(self, alarms_to_delete: list[str]) -> None:
        # interval for API calls
        interval = self._get_interval_in_sec()

        # delete up to 100 alarms per invocation
        chunk_size = 100
        for i in range(0, len(alarms_to_delete), chunk_size):
            end = i + chunk_size
            self.cloudwatch.delete_alarms(AlarmNames=alarms_to_delete[i:end])
            if interval > 0:
                sleep(interval)

    def _get_interval_in_sec(self) -> float:
        interval = self.config["globals"]["api_call_intervals_in_millis"]
        assert isinstance(interval, int)
        return interval / 1000  # from milli-seconds to seconds
