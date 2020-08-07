import logging

from rest_framework.generics import CreateAPIView, DestroyAPIView

from ..history.models import SafeContract
from .models import FirebaseDevice
from .serializers import FirebaseDeviceSerializer

logger = logging.getLogger(__name__)


class FirebaseDeviceCreateView(CreateAPIView):
    """
    Creates a new FirebaseDevice. If uuid is not provided a new device will be created.
    If a uuid for an existing Safe is provided the FirebaseDevice will be updated with all the new data provided.
    Safes provided on the request are always added and never removed/replaced
    """
    serializer_class = FirebaseDeviceSerializer


class FirebaseDeviceDeleteView(DestroyAPIView):
    """
    Remove a FirebaseDevice
    """
    queryset = FirebaseDevice.objects.all()


class FirebaseDeviceSafeDeleteView(DestroyAPIView):
    """
    Remove a Safe for a FirebaseDevice
    """
    queryset = FirebaseDevice.objects.all()

    def perform_destroy(self, firebase_device: FirebaseDevice):
        safe_address = self.kwargs['address']
        try:
            safe_contract = SafeContract.objects.get()
            firebase_device.safes.remove(safe_contract)
        except SafeContract.DoesNotExist:
            logger.info('Cannot remove safe=%s for firebase_device with uuid=%s', safe_address, self.kwargs['pk'])
