from rest_framework import serializers
from django.contrib.auth import get_user_model

User = get_user_model()

class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = [
            'id', 'username', 'email', 'first_name', 'last_name', 
            'role', 'roll_number', 'branch', 'year', 'phone_number',
            'github_profile', 'linkedin_profile', 'created_at'
        ]
        read_only_fields = ['id', 'role', 'created_at']

class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = ['username', 'email', 'password', 'first_name', 'last_name', 'roll_number', 'branch', 'year']

    def create(self, validated_data):
        user = User.objects.create_user(
            username=validated_data['username'],
            email=validated_data['email'],
            password=validated_data['password'],
            first_name=validated_data.get('first_name', ''),
            last_name=validated_data.get('last_name', ''),
            roll_number=validated_data.get('roll_number', ''),
            branch=validated_data.get('branch', ''),
            year=validated_data.get('year', None),
        )
        return user
