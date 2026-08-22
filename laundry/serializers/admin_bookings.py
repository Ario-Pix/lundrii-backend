"""Serializers for admin booking grid + analytics payloads."""

from rest_framework import serializers


class BookingChannelSerializer(serializers.Serializer):
    name = serializers.CharField()
    color = serializers.CharField()


class BookingCellSerializer(serializers.Serializer):
    machine_id = serializers.CharField()
    machine_label = serializers.CharField()
    hostel_name = serializers.CharField()
    hour = serializers.IntegerField()
    date = serializers.CharField()
    state = serializers.ChoiceField(
        choices=["open", "closed", "upcoming", "running", "completed"]
    )
    student_name = serializers.CharField(allow_null=True)
    student_id = serializers.CharField(allow_null=True)
    channel = BookingChannelSerializer(allow_null=True)
    slot_label = serializers.CharField()
    tip = serializers.CharField()
    booking_id = serializers.CharField(required=False)


class BookingDetailSerializer(serializers.Serializer):
    id = serializers.CharField()
    student_name = serializers.CharField()
    student_id = serializers.CharField(allow_null=True)
    machine_id = serializers.CharField()
    machine_label = serializers.CharField()
    hostel_name = serializers.CharField()
    starts_at = serializers.CharField()
    ends_at = serializers.CharField()
    channel = BookingChannelSerializer()
    booked_at_label = serializers.CharField()
    state = serializers.ChoiceField(
        choices=["open", "closed", "upcoming", "running", "completed"]
    )
    cancelled_at = serializers.CharField(allow_null=True)


class DemandHourPointSerializer(serializers.Serializer):
    hour = serializers.IntegerField()
    booked = serializers.IntegerField()
    turned_away = serializers.IntegerField()


class WeekdayPointSerializer(serializers.Serializer):
    label = serializers.CharField()
    booked = serializers.IntegerField()
    turned_away = serializers.IntegerField()


class ChannelShareSerializer(serializers.Serializer):
    name = serializers.CharField()
    color = serializers.CharField()
    pct = serializers.IntegerField()
    note = serializers.CharField()
    trend = serializers.CharField()
    up = serializers.BooleanField(allow_null=True)
