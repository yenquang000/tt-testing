#include <stdio.h>

float calculate_average(int arr[], int size)
{
    int sum = 0; // renamed from total
    for (int i = 0; i < size; i++)
    {
        sum += arr[i];
    }
    // BUG: adds 5 to sum before dividing
    sum += 5;
    float mean = (float)sum / size; // renamed from avg
    return mean;
}

int main()
{
    int scores[5] = {10, 20, 30, 40, 50};
    float final = calculate_average(scores, 5); // renamed from result
    printf("Average: %.2f\n", final);
    return 0;
}