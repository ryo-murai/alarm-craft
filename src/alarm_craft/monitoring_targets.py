import re
from abc import ABC, abstractmethod
from typing import Any, Callable, Generic, Iterable, Mapping, Optional, Protocol, Sequence, TypeVar

import boto3

from .models import MetricAlarmParam, ResourceConfig


def get_target_metrics(config: dict[str, Any]) -> Iterable[MetricAlarmParam]:
    """Gets target metrics

    Args:
        config (dict[str, Any]): config dict

    Yields:
        Iterator[Iterable[MetricAlarmParam]]: metric alarm params
    """
    for provider in _get_target_metrics_providers(config):
        yield from provider.get_metric_alarms()


# implementations


class TargetMetricsProvider(Protocol):
    """Target Metrics Provider

    Args:
        Protocol (_type_): protocol
    """

    def get_metric_alarms(self) -> Iterable[MetricAlarmParam]:
        """Gets metric alarms

        Returns:
            Iterable[MetricAlarmParam]: list of metric alarms
        """
        pass


def _get_target_metrics_providers(config: dict[str, Any]) -> Iterable[TargetMetricsProvider]:
    for resource_config_name, resource_config in config["resources"].items():
        resource_type = resource_config["target_resource_type"]

        if resource_type == "lambda:function":
            yield LambdaMetricsProvider(resource_config, resource_config_name)
        elif resource_type == "states:stateMachine":
            yield SfnMetricsProvider(resource_config, resource_config_name)
        elif resource_type == "apigateway:restapi":
            yield ApiGatewayMetricsProvider(resource_config, resource_config_name)
        elif resource_type == "sns:topic":
            yield SnsMetricsProvider(resource_config, resource_config_name)
        elif resource_type == "sqs:queue":
            yield SqsMetricsProvider(resource_config, resource_config_name)
        elif resource_type == "events:rule":
            yield EventBridgeMetricsProvider(resource_config, resource_config_name)
        else:
            raise ValueError(f"no such resource type: ${resource_type}")


T = TypeVar("T")


class TargetMetricsProviderBase(ABC, Generic[T]):
    """Target Metrics Provider Base

    Args:
        ABC (_type_): abstract class
        Generic (_type_): type
    """

    def __init__(self, resource_config: ResourceConfig, resource_config_name: str):
        """Constructor

        Args:
            resource_config (ResourceAlarmConfig): resource config dict
            resource_config_name (str): key of the resource config
        """
        self.resource_config = resource_config
        self.resource_config_name = resource_config_name

    def get_metric_alarms(self) -> Iterable[MetricAlarmParam]:
        """Gets metric alarms

        Yields:
            Iterator[Iterable[MetricAlarmParam]]: list of metric alarm params
        """
        for resource in self.get_monitoring_target_resources():
            for metric in self.metric_names(resource):
                param: MetricAlarmParam = {
                    "TargetResource": {
                        "ResourceName": self.get_resource_name(resource),
                    },
                    "AlarmProps": {
                        "MetricName": metric,
                        "Namespace": self.namespace(),
                        "Dimensions": self.dimensions(metric, resource),
                    },
                }

                param_overrides = self.param_overrides(metric, resource)
                if param_overrides:
                    # param_overrides is a AlarmProps ensured by jsonschema checking
                    param["AlarmProps"].update(param_overrides)  # type: ignore

                yield param

    @abstractmethod
    def get_monitoring_target_resources(self) -> Iterable[T]:
        """Gets monitoring target resources

        Returns:
            Iterable[T]: target resources
        """
        pass

    def namespace(self) -> str:
        """Gets alarm namespace

        Returns:
            str: alarm namespace
        """
        return str(self.resource_config["alarm"]["namespace"])

    def metric_names(self, resource: T) -> Sequence[str]:
        """Gets metric names

        Args:
            resource (T): resource

        Returns:
            list[str]: list of metric names
        """
        metrics = self.resource_config["alarm"]["metrics"]
        return metrics

    @abstractmethod
    def dimensions(self, metric_name: str, resource: T) -> Sequence[Mapping[str, str]]:
        """Gets alarm dimensions

        Args:
            metric_name (str): metric name
            resource (T): resource

        Returns:
            Sequence[Mapping[str, str]]: alarm dimensions
        """
        pass

    def param_overrides(self, metric_name: str, resource: T) -> Optional[Mapping[str, Any]]:
        """Gets alarm overrides

        Args:
            metric_name (str): metric name
            resource (T): resource

        Returns:
            Mapping[str, Any]: alarm param overrides
        """
        param_overrides = self.resource_config["alarm"].get("alarm_param_overrides")
        if param_overrides:
            return param_overrides.get(metric_name)

        return None

    @abstractmethod
    def get_resource_name(self, resource: T) -> str:
        """Gets resource name

        Args:
            resource (T): resource

        Returns:
            str: resource name
        """
        pass


class ResourceGroupsTaggingAPITargetMetricsProviderBase(TargetMetricsProviderBase[str]):
    """ResourceGroupsTaggingAPI target metrics provider base"""

    default_arn_pattern = re.compile("^arn:aws:[^:]*:[^:]*:[0-9]*:[^:]*:")
    arn_pattern_no_restype = re.compile("^arn:aws:[^:]*:[^:]*:[0-9]*:")
    arn_pattern_name_by_slash = re.compile("^arn:aws:[^:]*:[^:]*:[0-9]*:[^:]*/")

    def get_monitoring_target_resources(self) -> Iterable[str]:
        """Gets monitoring target resources

        Returns:
            Iterable[str]: monitoring target resources
        """
        resource_client = boto3.client("resourcegroupstaggingapi")
        try:
            tags = self.resource_config.get("target_resource_tags")
            pattern = self.resource_config.get("target_resource_name_pattern")
            resource_type = self.resource_config["target_resource_type"]
            request_param: dict = {
                "ResourceTypeFilters": [resource_type],
                "PaginationToken": "",
            }

            # tags condition
            if tags:
                tag_filters = [{"Key": k, "Values": [v]} for k, v in tags.items()]
                request_param["TagFilters"] = tag_filters

            # resource name pattern condition
            if pattern:
                filter_func = self._get_filter_by_resource_name_pattern(pattern)
            else:
                filter_func = ResourceGroupsTaggingAPITargetMetricsProviderBase._nofilter

            while True:
                resp = resource_client.get_resources(**request_param)

                yield from filter(filter_func, [r["ResourceARN"] for r in resp["ResourceTagMappingList"]])

                next_token = resp.get("PaginationToken")
                if next_token and next_token != "":
                    request_param["PaginationToken"] = next_token
                else:
                    # no more results
                    break

        finally:
            pass
            # resource_client.close()

    def get_resource_name(self, arn: str) -> str:
        """Gets resource name

        Args:
            arn (T): resource

        Returns:
            str: resource name
        """
        return self.default_arn_pattern.sub("", arn, 1)

    def _get_filter_by_resource_name_pattern(self, pattern: str) -> Callable[[str], bool]:
        regex = re.compile(pattern)

        def _filter_by_resource_name_pattern(arn: str) -> bool:
            return regex.match(self.get_resource_name(arn)) is not None

        return _filter_by_resource_name_pattern

    @classmethod
    def _nofilter(cls, _: str) -> bool:
        return True


class LambdaMetricsProvider(ResourceGroupsTaggingAPITargetMetricsProviderBase):
    """Lambda Metric Provider"""

    def dimensions(self, metric_name: str, arn: str) -> Sequence[Mapping[str, str]]:
        """Gets alarm dimensions

        Args:
            metric_name (str): metric name
            arn (T): resource

        Returns:
            Sequence[Mapping[str, str]]: alarm dimensions
        """
        name = self.get_resource_name(arn)
        return [{"Name": "FunctionName", "Value": name}]


class SfnMetricsProvider(ResourceGroupsTaggingAPITargetMetricsProviderBase):
    """StepFunctions Metrics Provider"""

    def dimensions(self, metric_name: str, arn: str) -> Sequence[Mapping[str, str]]:
        """Gets alarm dimensions

        Args:
            metric_name (str): metric name
            arn (T): resource

        Returns:
            Sequence[Mapping[str, str]]: alarm dimensions
        """
        return [{"Name": "StateMachineArn", "Value": arn}]


class SnsMetricsProvider(ResourceGroupsTaggingAPITargetMetricsProviderBase):
    """SNS Metrics Provider"""

    def get_resource_name(self, arn: str) -> str:
        """Gets resource name

        Args:
            arn (T): resource

        Returns:
            str: resource name
        """
        return self.arn_pattern_no_restype.sub("", arn, 1)

    def dimensions(self, metric_name: str, arn: str) -> Sequence[Mapping[str, str]]:
        """Gets alarm dimensions

        Args:
            metric_name (str): metric name
            arn (T): resource

        Returns:
            Sequence[Mapping[str, str]]: alarm dimensions
        """
        name = self.get_resource_name(arn)
        return [{"Name": "TopicName", "Value": name}]


class SqsMetricsProvider(ResourceGroupsTaggingAPITargetMetricsProviderBase):
    """SQS Metric Provider"""

    def get_resource_name(self, arn: str) -> str:
        """Gets resource name

        Args:
            arn (T): resource

        Returns:
            str: resource name
        """
        return self.arn_pattern_no_restype.sub("", arn, 1)

    def dimensions(self, metric_name: str, arn: str) -> Sequence[Mapping[str, str]]:
        """Gets alarm dimensions

        Args:
            metric_name (str): metric name
            arn (T): resource

        Returns:
            Sequence[Mapping[str, str]]: alarm dimensions
        """
        name = self.get_resource_name(arn)
        return [{"Name": "QueueName", "Value": name}]


class EventBridgeMetricsProvider(ResourceGroupsTaggingAPITargetMetricsProviderBase):
    """EventBridge Metrics Provider"""

    def get_resource_name(self, arn: str) -> str:
        """Gets resource name

        Args:
            arn (T): resource

        Returns:
            str: resource name
        """
        return self.arn_pattern_name_by_slash.sub("", arn, 1)

    def dimensions(self, metric_name: str, arn: str) -> Sequence[Mapping[str, str]]:
        """Gets alarm dimensions

        Args:
            metric_name (str): metric name
            arn (T): resource

        Returns:
            Sequence[Mapping[str, str]]: alarm dimensions
        """
        name = self.get_resource_name(arn)
        return [{"Name": "RuleName", "Value": name}]


class ApiGatewayMetricsProvider(TargetMetricsProviderBase[str]):
    """API Gateway Metrics Provider"""

    def get_monitoring_target_resources(self) -> Iterable[str]:
        """Gets monitoring target resources

        Returns:
            Iterable[str]: monitoring target resources
        """
        target_tags = self.resource_config.get("target_resource_tags")

        client = boto3.client("apigateway")
        try:
            resources = client.get_rest_apis()["items"]
            return [res["name"] for res in resources if self._contains_tags(res["tags"], target_tags)]

        finally:
            pass
            # client.close()

    def _contains_tags(self, actual_tags: Mapping[str, str], expected_tags: Optional[Mapping[str, str]]) -> bool:
        if expected_tags:
            tags = actual_tags.items()
            contains_all_expected_tags = all(tag in tags for tag in expected_tags.items())
            return contains_all_expected_tags
        else:
            return True

    def get_resource_name(self, api_name: str) -> str:
        """Gets resource name

        Args:
            api_name (T): resource

        Returns:
            str: resource name
        """
        return api_name

    def dimensions(self, metric_name: str, api_name: str) -> Sequence[Mapping[str, str]]:
        """Gets alarm dimensions

        Args:
            metric_name (str): metric name
            api_name (T): resource

        Returns:
            Sequence[Mapping[str, str]]: alarm dimensions
        """
        return [{"Name": "ApiName", "Value": api_name}]
